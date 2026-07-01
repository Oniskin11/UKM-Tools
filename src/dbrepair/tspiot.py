from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import posixpath
import re
import shlex
import threading

from . import webdist
from .config import AppConfig
from .remote import OperationCancelledError, RemoteClient


REMOTE_STATE_TIMEOUT_SECONDS = 60
DOWNLOAD_TIMEOUT_SECONDS = 300


class TsPiotError(RuntimeError):
    """Поднимается, когда шаг установки ТС ПИоТ не дал ожидаемого результата."""


@dataclass(frozen=True)
class TsPiotStep:
    step_id: str
    number: str
    title: str

    @property
    def display_name(self) -> str:
        return f"{self.number}. {self.title}"


TSPIOT_STEPS: tuple[TsPiotStep, ...] = (
    TsPiotStep("tspiot", "1", "Скачать tspiot и выставить права"),
    TsPiotStep("data_dir", "2", "Создать каталог data_tspiot"),
    TsPiotStep("kkt_driver", "3", "Обновить libsp-kkt-driver-*.so"),
    TsPiotStep("gismt_cert", "4", "Положить gismt_cert.txt в data_tspiot"),
)

DEFAULT_TARGET_BASES: tuple[str, ...] = ("/usr/local/ukmclient", "/usr/local/lillo")

ProgressCallback = Callable[[TsPiotStep, str, str | None], None]


def _download_command(url: str, dest: str) -> str:
    quoted_url = shlex.quote(url)
    quoted_dest = shlex.quote(dest)
    return (
        "if command -v curl >/dev/null 2>&1; then "
        f"curl -fsSL -o {quoted_dest} {quoted_url}; "
        "elif command -v wget >/dev/null 2>&1; then "
        f"wget -q -O {quoted_dest} {quoted_url}; "
        "else echo 'no curl/wget on host' >&2; exit 127; fi"
    )


def detect_environment(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
    candidate_bases: Sequence[str] = DEFAULT_TARGET_BASES,
) -> tuple[str, str | None, str]:
    """Определить архитектуру и каталог установки на удалённой кассе.

    Возвращает (architecture, target_base | None, raw_uname).
    """
    with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
        uname = remote.run("uname -m", check=False).stdout.strip()
        architecture = "x64" if "64" in uname.lower() else "x86"
        logger.info("Detected machine '%s' -> architecture %s", uname or "?", architecture)

        target_base: str | None = None
        for base in candidate_bases:
            result = remote.run(f"test -d {shlex.quote(base)}", use_sudo=True, check=False)
            if result.exit_status == 0:
                target_base = base
                logger.info("Detected target directory %s", base)
                break
        if target_base is None:
            logger.warning("No known target directory found among: %s", ", ".join(candidate_bases))

    return architecture, target_base, uname


def reboot_host(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    """Перезагрузить кассу. Соединение при этом обрывается — это ожидаемо."""
    with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
        logger.info("Sending reboot command to %s", config.connection.host)
        # Перезагрузка с задержкой в фоне, чтобы команда успела вернуться до разрыва SSH.
        remote.run(
            "(sleep 1; (reboot || shutdown -r now)) >/dev/null 2>&1 &",
            use_sudo=True,
            check=False,
            timeout=15,
        )
    logger.info("Reboot command sent.")


class TsPiotInstaller:
    def __init__(
        self,
        config: AppConfig,
        logger,
        *,
        base_url: str,
        architecture: str,
        target_base: str,
        cancel_event: threading.Event | None = None,
    ):
        if config.tspiot is None:
            raise TsPiotError("В config.toml отсутствует секция [tspiot].")
        self.config = config
        self.tspiot = config.tspiot
        self.logger = logger
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.architecture = architecture
        self.target_base = target_base.rstrip("/")
        self.owner = self.tspiot.resolve_owner(self.target_base)
        self.cancel_event = cancel_event

    @property
    def target_binary(self) -> str:
        return posixpath.join(self.target_base, self.tspiot.binary_name)

    @property
    def target_data_dir(self) -> str:
        return posixpath.join(self.target_base, self.tspiot.data_dir_name)

    def steps(self) -> tuple[TsPiotStep, ...]:
        return TSPIOT_STEPS

    def get_step(self, step_id: str) -> TsPiotStep:
        for step in TSPIOT_STEPS:
            if step.step_id == step_id:
                return step
        raise KeyError(f"Unknown TS PIoT step: {step_id}")

    def run(self, *, progress: ProgressCallback | None = None) -> None:
        self.run_steps([step.step_id for step in TSPIOT_STEPS], progress=progress)

    def run_step(self, step_id: str, *, progress: ProgressCallback | None = None) -> None:
        self.run_steps([step_id], progress=progress)

    def run_steps(
        self,
        step_ids: Sequence[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        if not step_ids:
            raise ValueError("At least one TS PIoT step must be provided.")

        steps = [self.get_step(step_id) for step_id in step_ids]

        self.logger.info("Starting TS PIoT installation on %s", self.config.connection.host)
        self.logger.info("Source web server: %s", self.base_url)
        self.logger.info("Target: %s (arch %s, owner %s)", self.target_base, self.architecture, self.owner)
        self._check_cancelled()
        with RemoteClient(self.config.connection, self.logger, cancel_event=self.cancel_event) as remote:
            for step in steps:
                self._check_cancelled()
                self.logger.info("Step %s: %s", step.number, step.title)
                if progress is not None:
                    progress(step, "running", None)
                try:
                    detail = self._execute_step(step.step_id, remote)
                except Exception as exc:
                    if progress is not None:
                        progress(step, "error", str(exc))
                    raise
                self._check_cancelled()
                if progress is not None:
                    progress(step, "success", detail)

        self.logger.info("TS PIoT installation completed successfully.")

    def _execute_step(self, step_id: str, remote: RemoteClient) -> str | None:
        handlers: dict[str, Callable[[RemoteClient], str | None]] = {
            "tspiot": self._step_tspiot,
            "data_dir": self._step_data_dir,
            "kkt_driver": self._step_kkt_driver,
            "gismt_cert": self._step_gismt_cert,
        }
        try:
            handler = handlers[step_id]
        except KeyError as exc:
            raise KeyError(f"Unknown TS PIoT step: {step_id}") from exc
        return handler(remote)

    def _step_tspiot(self, remote: RemoteClient) -> str | None:
        url, version = webdist.tspiot_url(self.base_url, self.architecture)
        self.logger.info("tspiot version %s: %s", version, url)
        expected = webdist.fetch_sha256(url)
        target = self.target_binary

        remote.run(f"mkdir -p {shlex.quote(self.target_base)}", use_sudo=True)
        changed = self._download_and_install(remote, url=url, dest=target, expected=expected)
        if not changed:
            return f"Уже актуально (v{version})"

        remote.run(f"chmod +x {shlex.quote(target)}", use_sudo=True)
        remote.run(f"chown {shlex.quote(self.owner)} {shlex.quote(target)}", use_sudo=True)
        self._wait_for_remote_path(remote, target, path_type="x", description=f"executable {target}")
        return f"Установлено (v{version})"

    def _step_data_dir(self, remote: RemoteClient) -> str | None:
        check = remote.run(
            f"test -d {shlex.quote(self.target_data_dir)}",
            use_sudo=True,
            check=False,
        )
        if check.exit_status == 0:
            self.logger.info("data_tspiot already exists, skipping.")
            return "Каталог уже существует"

        remote.run(f"mkdir -p {shlex.quote(self.target_data_dir)}", use_sudo=True)
        remote.run(
            f"chown {shlex.quote(self.owner)} {shlex.quote(self.target_data_dir)}",
            use_sudo=True,
        )
        self._wait_for_remote_path(
            remote,
            self.target_data_dir,
            path_type="d",
            description=f"directory {self.target_data_dir}",
        )
        return "Создан"

    def _step_kkt_driver(self, remote: RemoteClient) -> str | None:
        url, version, filename = webdist.kkt_driver_url(self.base_url, self.architecture)
        self.logger.info("KKT driver version %s: %s", version, url)
        expected = webdist.fetch_sha256(url)

        listing = remote.run(
            f"ls {shlex.quote(self.target_base)}/libsp-kkt-driver-*.so 2>/dev/null | head -n1",
            use_sudo=True,
            check=False,
        )
        existing = listing.stdout.strip()

        if existing:
            # Права и владелец снимаются заранее, чтобы вернуть их после замены (mv меняет inode).
            mode = remote.run(f"stat -c %a {shlex.quote(existing)}", use_sudo=True, check=False).stdout.strip()
            owner = remote.run(f"stat -c %U:%G {shlex.quote(existing)}", use_sudo=True, check=False).stdout.strip()
            target_so = existing
        else:
            mode = "0644"
            owner = self.owner
            target_so = posixpath.join(self.target_base, filename)
            self.logger.warning(
                "Existing libsp-kkt-driver-*.so not found, installing %s with owner %s (0644).",
                target_so,
                owner,
            )

        changed = self._download_and_install(remote, url=url, dest=target_so, expected=expected)
        if not changed:
            return f"Уже актуально (v{version})"

        if mode:
            remote.run(f"chmod {shlex.quote(mode)} {shlex.quote(target_so)}", use_sudo=True)
        if owner:
            remote.run(f"chown {shlex.quote(owner)} {shlex.quote(target_so)}", use_sudo=True)
        self._wait_for_remote_path(remote, target_so, path_type="f", description=f"KKT driver {target_so}")
        return f"Обновлено (v{version})"

    def _step_gismt_cert(self, remote: RemoteClient) -> str | None:
        url = webdist.gismt_cert_url(self.base_url, self.tspiot.gismt_cert_name)
        dest = posixpath.join(self.target_data_dir, self.tspiot.gismt_cert_name)
        self.logger.info("gismt cert: %s", url)
        expected = webdist.fetch_sha256(url)

        remote.run(f"mkdir -p {shlex.quote(self.target_data_dir)}", use_sudo=True)
        changed = self._download_and_install(remote, url=url, dest=dest, expected=expected)
        if not changed:
            return "Уже актуально"

        remote.run(f"chown {shlex.quote(self.owner)} {shlex.quote(dest)}", use_sudo=True)
        remote.run(f"chmod 0644 {shlex.quote(dest)}", use_sudo=True)
        self._wait_for_remote_path(remote, dest, path_type="f", description=f"gismt cert {dest}")
        return "Установлено"

    def _remote_sha256(self, remote: RemoteClient, path: str) -> str | None:
        quoted = shlex.quote(path)
        command = (
            f"f={quoted}; "
            '[ -e "$f" ] || exit 0; '
            'if command -v sha256sum >/dev/null 2>&1; then sha256sum "$f" 2>/dev/null; '
            'elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$f" 2>/dev/null; '
            'elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$f" 2>/dev/null; '
            "fi"
        )
        result = remote.run(command, use_sudo=True, check=False)
        match = re.search(r"[0-9a-fA-F]{64}", result.stdout)
        return match.group(0).lower() if match else None

    def _download_and_install(
        self,
        remote: RemoteClient,
        *,
        url: str,
        dest: str,
        expected: str | None,
    ) -> bool:
        """Скачать и установить файл ТОЛЬКО если он изменился. True, если заменён.

        Хэш проверяется ДО загрузки: сравнивается контрольная сумма с сервера
        (<url>.sha256) с sha256 уже установленного файла. Если совпали — загрузки нет.
        """
        installed = self._remote_sha256(remote, dest)
        if expected and installed == expected:
            self.logger.info("Файл не изменился (sha256 совпал), загрузка не требуется: %s", dest)
            return False
        if expected is None:
            self.logger.warning(
                "Нет .sha256 на сервере для %s: файл будет загружен без предварительной сверки.", url
            )

        tmp = dest + ".dbrepair.new"
        try:
            remote.run(
                _download_command(url, tmp),
                use_sudo=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            actual = self._remote_sha256(remote, tmp)
            if expected and actual and actual != expected:
                raise TsPiotError(
                    f"Контрольная сумма не совпала после загрузки {url} "
                    f"(ожидалось {expected}, получено {actual})."
                )
            # Атомарная замена: mv корректно работает даже с запущенным бинарником/занятой .so.
            remote.run(f"mv -f {shlex.quote(tmp)} {shlex.quote(dest)}", use_sudo=True)
            return True
        finally:
            remote.run(f"rm -f {shlex.quote(tmp)}", use_sudo=True, check=False)

    def _wait_for_remote_path(
        self,
        remote: RemoteClient,
        remote_path: str,
        *,
        path_type: str,
        description: str,
        timeout_seconds: int = REMOTE_STATE_TIMEOUT_SECONDS,
    ) -> None:
        self.logger.info("Waiting for %s.", description)
        command = (
            f"i=0; while [ $i -lt {timeout_seconds} ]; do "
            f"test -{path_type} {shlex.quote(remote_path)} && exit 0; "
            "sleep 1; i=$((i+1)); "
            "done; exit 1"
        )
        result = remote.run(
            command,
            use_sudo=True,
            check=False,
            timeout=timeout_seconds + 5,
        )
        self._check_cancelled()
        if result.exit_status != 0:
            raise TsPiotError(f"Timed out while waiting for {description}.")
        self.logger.info("Confirmed %s.", description)

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OperationCancelledError("Operation cancelled by user.")
