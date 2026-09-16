from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import posixpath
import shlex
import tarfile
import threading
import time

from .config import AppConfig
from .editors import set_innodb_force_recovery, update_db_ini
from .remote import OperationCancelledError, RemoteClient


SUCCESS_DUMP = "SUCCESS: DB dump complete"
SUCCESS_RESTORE = "SUCCESS: DB restore complete"
MYSQL_START_TIMEOUT_SECONDS = 120
MYSQL_STOP_TIMEOUT_SECONDS = 120
REMOTE_STATE_TIMEOUT_SECONDS = 60
LOCAL_STATE_TIMEOUT_SECONDS = 15
UKMCLIENT_START_TIMEOUT_SECONDS = 60
UKMCLIENT_STOP_TIMEOUT_SECONDS = 60
UKMCLIENT_START_SCRIPT_PATTERN = r"(^|/)ukmstart\.sh([[:space:]]|$)"


class WorkflowError(RuntimeError):
    """Raised when a required remote step did not produce the expected result."""


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    number: str
    title: str

    @property
    def display_name(self) -> str:
        return f"{self.number}. {self.title}"


WORKFLOW_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep("prepare", "1", "Загрузить архив и обновить db.ini"),
    WorkflowStep("backup_mysql", "2", "Остановить MySQL и создать mysql-db.tgz"),
    WorkflowStep("enable_recovery", "3", "Включить innodb_force_recovery=6 и запустить MySQL"),
    WorkflowStep("dump_db", "4", "Запустить dbdump.sh"),
    WorkflowStep("copy_dump", "4A", "Скопировать ukmclient.sql"),
    WorkflowStep("disable_recovery", "5", "Остановить MySQL и отключить innodb_force_recovery"),
    WorkflowStep(
        "replace_datadir",
        "6",
        "Удалить /usr/local/mysql/var, развернуть пустой datadir и запустить MySQL",
    ),
    WorkflowStep("restore_db", "7", "Запустить dbrestore.sh"),
    WorkflowStep("start_ukmclient", "8", "Запустить ukmclient"),
)


@dataclass(frozen=True)
class WorkflowSession:
    timestamp: str
    local_dump_copy: Path
    remote_dump_copy: str
    remote_mysql_backup: str


@dataclass(frozen=True)
class WorkflowArtifacts:
    local_dump_copy: Path
    remote_dump_copy: str
    remote_mysql_backup: str


ProgressCallback = Callable[[WorkflowStep, str, str | None], None]


UKMCLIENT_OFFLINE_STEP_IDS = frozenset(
    {
        "backup_mysql",
        "enable_recovery",
        "dump_db",
        "disable_recovery",
        "replace_datadir",
        "restore_db",
    }
)


class DbRepairWorkflow:
    def __init__(
        self,
        config: AppConfig,
        logger,
        *,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.logger = logger
        self.cancel_event = cancel_event
        self._verified_dump_hashes: dict[Path, str] = {}

    def steps(self) -> tuple[WorkflowStep, ...]:
        return WORKFLOW_STEPS

    def get_step(self, step_id: str) -> WorkflowStep:
        for step in WORKFLOW_STEPS:
            if step.step_id == step_id:
                return step
        raise KeyError(f"Unknown workflow step: {step_id}")

    def create_session(self, timestamp: str | None = None) -> WorkflowSession:
        timestamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        local_backup_root = self.config.paths.local_backup_dir / self.config.connection.host
        local_dump_copy = local_backup_root / f"{timestamp}-{self.config.paths.remote_dump_filename}"
        remote_dump_copy = posixpath.join(
            self.config.paths.remote_dbrepair_dir,
            f"{timestamp}-{self.config.paths.remote_dump_filename}",
        )
        remote_mysql_backup = posixpath.join(
            self.config.paths.remote_mysql_dir,
            _timestamped_name(self.config.paths.remote_mysql_backup_name, timestamp),
        )
        return WorkflowSession(
            timestamp=timestamp,
            local_dump_copy=local_dump_copy,
            remote_dump_copy=remote_dump_copy,
            remote_mysql_backup=remote_mysql_backup,
        )

    def run(
        self,
        *,
        session: WorkflowSession | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkflowArtifacts:
        return self.run_steps(
            [step.step_id for step in WORKFLOW_STEPS],
            session=session,
            progress=progress,
        )

    def run_step(
        self,
        step_id: str,
        *,
        session: WorkflowSession | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkflowArtifacts:
        return self.run_steps([step_id], session=session, progress=progress)

    def run_steps(
        self,
        step_ids: Sequence[str],
        *,
        session: WorkflowSession | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkflowArtifacts:
        if not step_ids:
            raise ValueError("At least one workflow step must be provided.")
        self._validate_step_plan(step_ids)

        session = session or self.create_session()
        steps = [self.get_step(step_id) for step_id in step_ids]

        self.logger.info("Starting workflow for %s", self.config.connection.host)
        self._check_cancelled()
        with RemoteClient(self.config.connection, self.logger, cancel_event=self.cancel_event) as remote:
            recovery_enabled = False
            try:
                for step in steps:
                    self._check_cancelled()
                    self.logger.info("Step %s: %s", step.number, step.title)
                    if progress is not None:
                        progress(step, "running", None)
                    if step.step_id == "enable_recovery":
                        recovery_enabled = True
                    try:
                        self._execute_step(step.step_id, remote, session)
                    except Exception as exc:
                        if progress is not None:
                            progress(step, "error", str(exc))
                        raise
                    if step.step_id == "disable_recovery":
                        recovery_enabled = False
                    self._check_cancelled()
                    if progress is not None:
                        progress(step, "success", None)
            except Exception:
                if recovery_enabled:
                    self._emergency_disable_recovery(remote)
                raise

        self.logger.info("Workflow completed successfully.")
        return self._build_artifacts(session)

    def _build_artifacts(self, session: WorkflowSession) -> WorkflowArtifacts:
        return WorkflowArtifacts(
            local_dump_copy=session.local_dump_copy,
            remote_dump_copy=session.remote_dump_copy,
            remote_mysql_backup=session.remote_mysql_backup,
        )

    def _validate_step_plan(self, step_ids: Sequence[str]) -> None:
        """Destructive replacement is allowed only in a complete, ordered recovery plan."""
        if "replace_datadir" not in step_ids:
            return
        required = [step.step_id for step in WORKFLOW_STEPS]
        if list(step_ids) != required:
            raise WorkflowError(
                "replace_datadir may only run as part of the complete recovery workflow; "
                "use Run all steps."
            )

    def _execute_step(
        self,
        step_id: str,
        remote: RemoteClient,
        session: WorkflowSession,
    ) -> None:
        handlers: dict[str, Callable[[RemoteClient, WorkflowSession], None]] = {
            "prepare": self._step_prepare,
            "backup_mysql": self._step_backup_mysql,
            "enable_recovery": self._step_enable_recovery,
            "dump_db": self._step_dump_db,
            "copy_dump": self._step_copy_dump,
            "disable_recovery": self._step_disable_recovery,
            "replace_datadir": self._step_replace_datadir,
            "restore_db": self._step_restore_db,
            "start_ukmclient": self._step_start_ukmclient,
        }
        try:
            handler = handlers[step_id]
        except KeyError as exc:
            raise KeyError(f"Unknown workflow step: {step_id}") from exc
        if step_id in UKMCLIENT_OFFLINE_STEP_IDS:
            self._ensure_ukmclient_stopped(remote)
        handler(remote, session)

    def _step_prepare(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        paths = self.config.paths
        db = self.config.database

        if not paths.dbrepair_archive.is_file():
            raise WorkflowError(
                f"Архив dbrepair не найден: {paths.dbrepair_archive} "
                "(проверьте paths.dbrepair_archive в config.toml)."
            )

        remote.run(f"mkdir -p {shlex.quote(paths.remote_tmp_dir)}")
        self._wait_for_remote_path(
            remote,
            paths.remote_tmp_dir,
            path_type="d",
            description=f"directory {paths.remote_tmp_dir}",
        )
        remote.upload(paths.dbrepair_archive, paths.remote_dbrepair_archive)
        self._wait_for_remote_path(
            remote,
            paths.remote_dbrepair_archive,
            path_type="f",
            description=f"archive {paths.remote_dbrepair_archive}",
        )
        remote.run(f"rm -rf {shlex.quote(paths.remote_dbrepair_dir)}")
        self._wait_for_remote_absent(
            remote,
            paths.remote_dbrepair_dir,
            description=f"directory {paths.remote_dbrepair_dir}",
        )
        remote.run(
            f"tar xzf {shlex.quote(paths.remote_dbrepair_archive)} -C {shlex.quote(paths.remote_tmp_dir)}"
        )
        self._wait_for_remote_path(
            remote,
            paths.remote_dbrepair_dir,
            path_type="d",
            description=f"directory {paths.remote_dbrepair_dir}",
        )
        self._wait_for_remote_path(
            remote,
            paths.remote_db_ini,
            path_type="f",
            description=f"file {paths.remote_db_ini}",
        )
        self._wait_for_remote_path(
            remote,
            paths.remote_dbdump_script,
            path_type="f",
            description=f"file {paths.remote_dbdump_script}",
        )
        self._wait_for_remote_path(
            remote,
            paths.remote_dbrestore_script,
            path_type="f",
            description=f"file {paths.remote_dbrestore_script}",
        )

        db_ini = remote.read_text(paths.remote_db_ini)
        remote.write_text(paths.remote_db_ini, update_db_ini(db_ini, db.name, db.password))
        self._wait_for_remote_regex(
            remote,
            paths.remote_db_ini,
            r"^[[:space:]]*export[[:space:]]+DBNAME=",
            description=f"DBNAME in {paths.remote_db_ini}",
        )
        self._wait_for_remote_regex(
            remote,
            paths.remote_db_ini,
            r"^[[:space:]]*export[[:space:]]+DBPASSWORD=",
            description=f"DBPASSWORD in {paths.remote_db_ini}",
        )

    def _step_backup_mysql(self, remote: RemoteClient, session: WorkflowSession) -> None:
        paths = self.config.paths
        services = self.config.services

        remote.run(services.mysql_stop, use_sudo=True)
        self._wait_for_mysql_stopped(remote)
        remote.run(
            f"tar czf {shlex.quote(session.remote_mysql_backup)} var",
            cwd=paths.remote_mysql_dir,
            use_sudo=True,
        )
        remote.run(f"tar tzf {shlex.quote(session.remote_mysql_backup)} >/dev/null", use_sudo=True)
        self._wait_for_remote_path(
            remote,
            session.remote_mysql_backup,
            path_type="f",
            description=f"backup {paths.remote_mysql_backup}",
            use_sudo=True,
        )

    def _step_enable_recovery(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        my_cnf = remote.read_text(self.config.paths.remote_my_cnf, use_sudo=True)
        updated = set_innodb_force_recovery(my_cnf, enabled=True)
        remote.write_text(self.config.paths.remote_my_cnf, updated, use_sudo=True)
        self._wait_for_remote_regex(
            remote,
            self.config.paths.remote_my_cnf,
            r"^[[:space:]]*set-variable=innodb_force_recovery=6[[:space:]]*$",
            description=f"enabled innodb_force_recovery in {self.config.paths.remote_my_cnf}",
            use_sudo=True,
        )
        remote.run(self.config.services.mysql_start, use_sudo=True)
        self._wait_for_mysql_started(remote)

    def _step_dump_db(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        result = remote.run(
            "chmod +x dbdump.sh && ./dbdump.sh",
            cwd=self.config.paths.remote_dbrepair_dir,
            use_sudo=True,
        )
        if SUCCESS_DUMP not in result.stdout and SUCCESS_DUMP not in result.stderr:
            raise WorkflowError("dbdump.sh completed without success marker.")
        self._wait_for_remote_path(
            remote,
            self.config.paths.remote_dump_file,
            path_type="f",
            description=f"dump file {self.config.paths.remote_dump_file}",
            use_sudo=True,
        )
        self._wait_for_remote_condition(
            remote,
            _wait_for_remote_test_command(
                f"test -s {shlex.quote(self.config.paths.remote_dump_file)}",
                timeout_seconds=REMOTE_STATE_TIMEOUT_SECONDS,
            ),
            description=f"non-empty dump file {self.config.paths.remote_dump_file}",
            timeout_seconds=REMOTE_STATE_TIMEOUT_SECONDS,
            use_sudo=True,
        )

    def _step_copy_dump(self, remote: RemoteClient, session: WorkflowSession) -> None:
        dump_file = self.config.paths.remote_dump_file
        remote.run(
            f"cp {shlex.quote(dump_file)} {shlex.quote(session.remote_dump_copy)}"
            f" && chmod 0644 {shlex.quote(session.remote_dump_copy)}",
            use_sudo=True,
        )
        self._wait_for_remote_path(
            remote,
            session.remote_dump_copy,
            path_type="f",
            description=f"remote dump copy {session.remote_dump_copy}",
            use_sudo=True,
        )
        remote.download(dump_file, session.local_dump_copy, use_sudo=True)
        self._wait_for_local_path(
            session.local_dump_copy,
            description=f"local dump copy {session.local_dump_copy}",
        )
        remote_hash = _read_sha256(remote, dump_file)
        local_hash = _local_sha256(session.local_dump_copy)
        if remote_hash != local_hash:
            raise WorkflowError("Downloaded SQL dump checksum does not match the remote file.")
        self._verified_dump_hashes[session.local_dump_copy] = local_hash

    def _step_disable_recovery(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        remote.run(self.config.services.mysql_stop, use_sudo=True)
        self._wait_for_mysql_stopped(remote)
        my_cnf = remote.read_text(self.config.paths.remote_my_cnf, use_sudo=True)
        updated = set_innodb_force_recovery(my_cnf, enabled=False)
        remote.write_text(self.config.paths.remote_my_cnf, updated, use_sudo=True)
        self._wait_for_remote_regex(
            remote,
            self.config.paths.remote_my_cnf,
            r"^[[:space:]]*#[[:space:]]*set-variable=innodb_force_recovery=6[[:space:]]*$",
            description=f"disabled innodb_force_recovery in {self.config.paths.remote_my_cnf}",
            use_sudo=True,
        )

    def _step_replace_datadir(self, remote: RemoteClient, session: WorkflowSession) -> None:
        paths = self.config.paths

        if not paths.empty_datadir_archive.is_file():
            raise WorkflowError(
                f"Архив пустого datadir не найден: {paths.empty_datadir_archive} "
                "(проверьте paths.empty_datadir_archive в config.toml)."
            )
        _validate_dbrepair_archive(paths.dbrepair_archive, paths.remote_dbrepair_dir_name)
        _validate_empty_datadir_archive(paths.empty_datadir_archive)
        if session.local_dump_copy not in self._verified_dump_hashes:
            raise WorkflowError("Refusing to replace datadir: the SQL dump was not verified in this workflow session.")

        remote.run(f"rm -rf {shlex.quote(paths.remote_mysql_var_dir)}", use_sudo=True)
        self._wait_for_remote_absent(
            remote,
            paths.remote_mysql_var_dir,
            description=f"directory {paths.remote_mysql_var_dir}",
            use_sudo=True,
        )
        # Copy mysql5-datadir-empty_46+.tgz into /usr/local/mysql on the cash register.
        remote.upload(paths.empty_datadir_archive, paths.remote_empty_datadir_archive, use_sudo=True)
        self._wait_for_remote_path(
            remote,
            paths.remote_empty_datadir_archive,
            path_type="f",
            description=f"archive {paths.remote_empty_datadir_archive}",
            use_sudo=True,
        )
        # Extract the empty datadir archive directly into /usr/local/mysql.
        remote.run(
            f"tar xzf {shlex.quote(paths.remote_empty_datadir_archive)} -C {shlex.quote(paths.remote_mysql_dir)}",
            use_sudo=True,
        )
        self._wait_for_remote_path(
            remote,
            paths.remote_mysql_var_dir,
            path_type="d",
            description=f"directory {paths.remote_mysql_var_dir}",
            use_sudo=True,
        )
        remote.run(self.config.services.mysql_start, use_sudo=True)
        self._wait_for_mysql_started(remote)

    def _step_restore_db(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        result = remote.run(
            "chmod +x dbrestore.sh && ./dbrestore.sh",
            cwd=self.config.paths.remote_dbrepair_dir,
            use_sudo=True,
        )
        if SUCCESS_RESTORE not in result.stdout and SUCCESS_RESTORE not in result.stderr:
            raise WorkflowError("dbrestore.sh completed without success marker.")
        self._wait_for_mysql_started(remote)

    def _emergency_disable_recovery(self, remote: RemoteClient) -> None:
        """Best-effort cleanup that never hides the original workflow failure."""
        self.logger.warning("Workflow failed with innodb_force_recovery enabled; attempting emergency cleanup.")
        try:
            remote.run(self.config.services.mysql_stop, use_sudo=True, check=False)
            my_cnf = remote.read_text(self.config.paths.remote_my_cnf, use_sudo=True)
            remote.write_text(
                self.config.paths.remote_my_cnf,
                set_innodb_force_recovery(my_cnf, enabled=False),
                use_sudo=True,
            )
            remote.run(self.config.services.mysql_start, use_sudo=True, check=False)
        except Exception as exc:
            self.logger.warning("Emergency recovery cleanup failed: %s", exc)

    def _step_start_ukmclient(self, remote: RemoteClient, session: WorkflowSession) -> None:
        del session
        remote.run(self.config.services.ukmclient_start, use_sudo=True)
        self._wait_for_ukmclient_started(remote)

    def _ensure_ukmclient_stopped(self, remote: RemoteClient) -> None:
        self.logger.info("Stopping ukmclient before database operations.")
        remote.run(self.config.services.ukmclient_stop, use_sudo=True, check=False)
        try:
            self._wait_for_ukmclient_stopped(remote)
            return
        except WorkflowError:
            self.logger.warning("ukmclient is still running after service stop, terminating residual processes.")

        remote.run(
            _terminate_ukmclient_processes_command(force=False),
            use_sudo=True,
            check=False,
        )
        try:
            self._wait_for_ukmclient_stopped(remote)
            return
        except WorkflowError:
            self.logger.warning("ukmclient is still running after TERM, sending SIGKILL.")

        remote.run(
            _terminate_ukmclient_processes_command(force=True),
            use_sudo=True,
            check=False,
        )
        self._wait_for_ukmclient_stopped(remote)

    def _wait_for_mysql_started(self, remote: RemoteClient) -> None:
        self.logger.info("Waiting for MySQL to become ready.")
        self._wait_for_remote_condition(
            remote,
            _wait_for_mysql_command(running=True, timeout_seconds=MYSQL_START_TIMEOUT_SECONDS),
            description="MySQL ready state",
            timeout_seconds=MYSQL_START_TIMEOUT_SECONDS,
            use_sudo=True,
        )
        self.logger.info("MySQL is ready.")

    def _wait_for_mysql_stopped(self, remote: RemoteClient) -> None:
        self.logger.info("Waiting for MySQL to stop completely.")
        self._wait_for_remote_condition(
            remote,
            _wait_for_mysql_command(running=False, timeout_seconds=MYSQL_STOP_TIMEOUT_SECONDS),
            description="MySQL stopped state",
            timeout_seconds=MYSQL_STOP_TIMEOUT_SECONDS,
            use_sudo=True,
        )
        self.logger.info("MySQL is stopped.")

    def _wait_for_ukmclient_started(self, remote: RemoteClient) -> None:
        self.logger.info("Waiting for ukmclient to start.")
        self._wait_for_remote_condition(
            remote,
            _wait_for_ukmclient_command(running=True, timeout_seconds=UKMCLIENT_START_TIMEOUT_SECONDS),
            description="ukmclient started state",
            timeout_seconds=UKMCLIENT_START_TIMEOUT_SECONDS,
            use_sudo=True,
        )
        self.logger.info("ukmclient is running.")

    def _wait_for_ukmclient_stopped(self, remote: RemoteClient) -> None:
        self.logger.info("Waiting for ukmclient to stop completely.")
        self._wait_for_remote_condition(
            remote,
            _wait_for_ukmclient_command(running=False, timeout_seconds=UKMCLIENT_STOP_TIMEOUT_SECONDS),
            description="ukmclient stopped state",
            timeout_seconds=UKMCLIENT_STOP_TIMEOUT_SECONDS,
            use_sudo=True,
        )
        self.logger.info("ukmclient is stopped.")

    def _wait_for_remote_path(
        self,
        remote: RemoteClient,
        remote_path: str,
        *,
        path_type: str,
        description: str,
        timeout_seconds: int = REMOTE_STATE_TIMEOUT_SECONDS,
        use_sudo: bool = False,
    ) -> None:
        self._wait_for_remote_condition(
            remote,
            _wait_for_remote_test_command(
                f"test -{path_type} {shlex.quote(remote_path)}",
                timeout_seconds=timeout_seconds,
            ),
            description=description,
            timeout_seconds=timeout_seconds,
            use_sudo=use_sudo,
        )

    def _wait_for_remote_absent(
        self,
        remote: RemoteClient,
        remote_path: str,
        *,
        description: str,
        timeout_seconds: int = REMOTE_STATE_TIMEOUT_SECONDS,
        use_sudo: bool = False,
    ) -> None:
        self._wait_for_remote_condition(
            remote,
            _wait_for_remote_test_command(
                f"test ! -e {shlex.quote(remote_path)}",
                timeout_seconds=timeout_seconds,
            ),
            description=f"absence of {description}",
            timeout_seconds=timeout_seconds,
            use_sudo=use_sudo,
        )

    def _wait_for_remote_text(
        self,
        remote: RemoteClient,
        remote_path: str,
        needle: str,
        *,
        description: str,
        timeout_seconds: int = REMOTE_STATE_TIMEOUT_SECONDS,
        use_sudo: bool = False,
    ) -> None:
        self._wait_for_remote_condition(
            remote,
            _wait_for_remote_test_command(
                f"grep -Fq -- {shlex.quote(needle)} {shlex.quote(remote_path)}",
                timeout_seconds=timeout_seconds,
            ),
            description=description,
            timeout_seconds=timeout_seconds,
            use_sudo=use_sudo,
        )

    def _wait_for_remote_regex(
        self,
        remote: RemoteClient,
        remote_path: str,
        pattern: str,
        *,
        description: str,
        timeout_seconds: int = REMOTE_STATE_TIMEOUT_SECONDS,
        use_sudo: bool = False,
    ) -> None:
        self._wait_for_remote_condition(
            remote,
            _wait_for_remote_test_command(
                f"grep -Eq -- {shlex.quote(pattern)} {shlex.quote(remote_path)}",
                timeout_seconds=timeout_seconds,
            ),
            description=description,
            timeout_seconds=timeout_seconds,
            use_sudo=use_sudo,
        )

    def _wait_for_remote_condition(
        self,
        remote: RemoteClient,
        command: str,
        *,
        description: str,
        timeout_seconds: int,
        use_sudo: bool = False,
    ) -> None:
        self.logger.info("Waiting for %s.", description)
        result = remote.run(
            command,
            use_sudo=use_sudo,
            check=False,
            timeout=timeout_seconds + 5,
        )
        self._check_cancelled()
        if result.exit_status != 0:
            raise WorkflowError(f"Timed out while waiting for {description}.")
        self.logger.info("Confirmed %s.", description)

    def _wait_for_local_path(
        self,
        local_path: Path,
        *,
        description: str,
        timeout_seconds: int = LOCAL_STATE_TIMEOUT_SECONDS,
    ) -> None:
        self.logger.info("Waiting for %s.", description)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            self._check_cancelled()
            if local_path.exists():
                self.logger.info("Confirmed %s.", description)
                return
            time.sleep(0.2)
        raise WorkflowError(f"Timed out while waiting for {description}.")

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OperationCancelledError("Operation cancelled by user.")


def _wait_for_mysql_command(*, running: bool, timeout_seconds: int) -> str:
    if running:
        return (
            "if command -v mysqladmin >/dev/null 2>&1; then "
            f"i=0; while [ $i -lt {timeout_seconds} ]; do "
            "mysqladmin ping >/dev/null 2>&1 && exit 0; "
            "sleep 1; i=$((i+1)); "
            "done; exit 1; "
            "fi; "
            f"i=0; while [ $i -lt {timeout_seconds} ]; do "
            "pidof mysqld >/dev/null 2>&1 && exit 0; "
            "sleep 1; i=$((i+1)); "
            "done; exit 1"
        )

    return (
        f"i=0; while [ $i -lt {timeout_seconds} ]; do "
        "pidof mysqld >/dev/null 2>&1 || exit 0; "
        "sleep 1; i=$((i+1)); "
        "done; exit 1"
    )


def _wait_for_remote_test_command(test_command: str, *, timeout_seconds: int) -> str:
    return (
        f"i=0; while [ $i -lt {timeout_seconds} ]; do "
        f"{test_command} && exit 0; "
        "sleep 1; i=$((i+1)); "
        "done; exit 1"
    )


def _wait_for_ukmclient_command(*, running: bool, timeout_seconds: int) -> str:
    process_check = (
        "("
        "pidof ukmclient >/dev/null 2>&1 "
        "|| pidof cashmain >/dev/null 2>&1 "
        f"|| pgrep -f {shlex.quote(UKMCLIENT_START_SCRIPT_PATTERN)} >/dev/null 2>&1"
        ")"
    )
    if running:
        condition = f"{process_check} && exit 0"
    else:
        condition = f"{process_check} || exit 0"

    return (
        f"i=0; while [ $i -lt {timeout_seconds} ]; do "
        f"{condition}; "
        "sleep 1; i=$((i+1)); "
        "done; exit 1"
    )


def _terminate_ukmclient_processes_command(*, force: bool) -> str:
    signal = "-9 " if force else ""
    start_script = shlex.quote(UKMCLIENT_START_SCRIPT_PATTERN)
    return (
        f"pkill {signal}-x ukmclient >/dev/null 2>&1 || true; "
        f"pkill {signal}-x cashmain >/dev/null 2>&1 || true; "
        f"pkill {signal}-f {start_script} >/dev/null 2>&1 || true"
    )


def _timestamped_name(name: str, timestamp: str) -> str:
    path = Path(name)
    suffix = "".join(path.suffixes)
    stem = name[: -len(suffix)] if suffix else name
    return f"{stem}-{timestamp}{suffix}"


def _local_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sha256(remote: RemoteClient, remote_path: str) -> str:
    quoted = shlex.quote(remote_path)
    result = remote.run(
        "if command -v sha256sum >/dev/null 2>&1; then "
        f"sha256sum {quoted}; "
        "elif command -v openssl >/dev/null 2>&1; then "
        f"openssl dgst -sha256 {quoted}; "
        "else exit 127; fi",
        use_sudo=True,
    )
    for token in result.stdout.split():
        if len(token) == 64 and all(char in "0123456789abcdefABCDEF" for char in token):
            return token.lower()
    raise WorkflowError(f"Could not read SHA-256 for {remote_path}.")


def _validate_empty_datadir_archive(path: Path) -> None:
    """The archive may create only var/ below the selected MySQL directory."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise WorkflowError(f"Invalid empty datadir archive: {path}") from exc
    if not members:
        raise WorkflowError(f"Empty datadir archive has no files: {path}")
    for member in members:
        name = member.name.replace("\\", "/").lstrip("./")
        if not name.startswith("var/") and name != "var":
            raise WorkflowError("Empty datadir archive may contain only the var directory.")
        if member.issym() or member.islnk() or ".." in name.split("/"):
            raise WorkflowError("Empty datadir archive contains an unsafe link or path.")


def _validate_dbrepair_archive(path: Path, directory_name: str) -> None:
    """Reject path traversal and require the three scripts used by the workflow."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = set()
            for member in archive.getmembers():
                name = member.name.replace("\\", "/").lstrip("./")
                if not name or name.startswith("/") or ".." in name.split("/") or member.issym() or member.islnk():
                    raise WorkflowError("dbrepair archive contains an unsafe path or link.")
                names.add(name)
    except (tarfile.TarError, OSError) as exc:
        raise WorkflowError(f"Invalid dbrepair archive: {path}") from exc
    required = {f"{directory_name}/{name}" for name in ("db.ini", "dbdump.sh", "dbrestore.sh")}
    if not required.issubset(names):
        raise WorkflowError("dbrepair archive does not contain db.ini, dbdump.sh and dbrestore.sh in its root directory.")
