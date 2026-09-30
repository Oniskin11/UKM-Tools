from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import posixpath
import re
import shlex
import threading

from .config import AppConfig
from .distsource import DistSource
from .remote import OperationCancelledError, RemoteClient


REMOTE_STATE_TIMEOUT_SECONDS = 60


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


def _last_line(text: str) -> str:
    """Последняя непустая строка вывода (защита от баннеров login-shell в stdout)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _valid_owner_or_default(owner: str, default_owner: str) -> str:
    """Return a usable name:group pair, falling back after a POS reinstall.

    ``stat -c %U:%G`` prints ``UNKNOWN:UNKNOWN`` for orphaned numeric ids.
    Such a value cannot be passed back to chown.  The installation directory
    owner is the safe, configured fallback for a freshly installed POS.
    """
    if owner.upper() != "UNKNOWN:UNKNOWN" and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*", owner
    ):
        return owner
    return default_owner


def _detect_on(
    remote: RemoteClient,
    logger,
    candidate_bases: Sequence[str],
) -> tuple[str, str | None, str]:
    """Определить архитектуру и каталог установки, используя открытое соединение."""
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
        return _detect_on(remote, logger, candidate_bases)


def reboot_host(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    """Перезагрузить кассу. Соединение при этом обрывается — это ожидаемо."""
    with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
        logger.info("Sending reboot command to %s", config.connection.host)
        # setsid + nohup, чтобы фоновая перезагрузка пережила закрытие SSH-сессии (SIGHUP).
        remote.run(
            "setsid sh -c 'sleep 2; reboot || shutdown -r now' >/dev/null 2>&1 < /dev/null &",
            use_sudo=True,
            check=False,
            timeout=15,
        )
    logger.info("Reboot command sent.")


EnvironmentCallback = Callable[[str, str | None], None]


class TsPiotInstaller:
    def __init__(
        self,
        config: AppConfig,
        logger,
        *,
        source: DistSource,
        architecture: str,
        target_base: str,
        cancel_event: threading.Event | None = None,
        auto_detect: bool = False,
        on_detect: EnvironmentCallback | None = None,
        candidate_bases: Sequence[str] = DEFAULT_TARGET_BASES,
    ):
        if config.tspiot is None:
            raise TsPiotError("В config.toml отсутствует секция [tspiot].")
        self.config = config
        self.tspiot = config.tspiot
        self.logger = logger
        self.source = source
        self.architecture = architecture
        self.target_base = target_base.rstrip("/")
        self.owner = self.tspiot.resolve_owner(self.target_base)
        self.cancel_event = cancel_event
        self.auto_detect = auto_detect
        self.on_detect = on_detect
        self.candidate_bases = candidate_bases

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
        self.logger.info("Source: %s", self.source.describe())
        self._check_cancelled()
        with RemoteClient(self.config.connection, self.logger, cancel_event=self.cancel_event) as remote:
            if self.auto_detect:
                arch, target, _ = _detect_on(remote, self.logger, self.candidate_bases)
                if arch:
                    self.architecture = arch
                if target:
                    self.target_base = target.rstrip("/")
                self.owner = self.tspiot.resolve_owner(self.target_base)
                if self.on_detect is not None:
                    self.on_detect(self.architecture, target)
            self.logger.info(
                "Target: %s (arch %s, owner %s)", self.target_base, self.architecture, self.owner
            )
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
        artifact = self.source.tspiot(self.architecture)
        self.logger.info("tspiot version %s: %s", artifact.version, artifact.locator)
        target = self.target_binary

        remote.run(f"mkdir -p {shlex.quote(self.target_base)}", use_sudo=True)
        changed = self._install(remote, artifact, target)
        if not changed:
            return f"Уже актуально (v{artifact.version})"

        remote.run(f"chmod +x {shlex.quote(target)}", use_sudo=True)
        remote.run(f"chown {shlex.quote(self.owner)} {shlex.quote(target)}", use_sudo=True)
        self._wait_for_remote_path(remote, target, path_type="x", description=f"executable {target}")
        return f"Установлено (v{artifact.version})"

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
        artifact = self.source.kkt_driver(self.architecture)
        self.logger.info("KKT driver version %s: %s", artifact.version, artifact.locator)

        listing = remote.run(
            f"ls {shlex.quote(self.target_base)}/libsp-kkt-driver-*.so 2>/dev/null | head -n1",
            use_sudo=True,
            check=False,
        )
        existing = _last_line(listing.stdout)

        if existing:
            # Права и владелец снимаются заранее, чтобы вернуть их после замены (mv меняет inode).
            mode = _last_line(remote.run(f"stat -c %a {shlex.quote(existing)}", use_sudo=True, check=False).stdout)
            detected_owner = _last_line(
                remote.run(f"stat -c %U:%G {shlex.quote(existing)}", use_sudo=True, check=False).stdout
            )
            owner = _valid_owner_or_default(detected_owner, self.owner)
            if owner != detected_owner:
                self.logger.warning(
                    "Driver owner %r is unavailable; using target owner %s.",
                    detected_owner or "<empty>",
                    owner,
                )
            target_so = existing
        else:
            mode = "0644"
            owner = self.owner
            target_so = posixpath.join(self.target_base, artifact.filename)
            self.logger.warning(
                "Existing libsp-kkt-driver-*.so not found, installing %s with owner %s (0644).",
                target_so,
                owner,
            )

        changed = self._install(remote, artifact, target_so)
        if not changed:
            return f"Уже актуально (v{artifact.version})"

        if mode:
            remote.run(f"chmod {shlex.quote(mode)} {shlex.quote(target_so)}", use_sudo=True)
        if owner:
            remote.run(f"chown {shlex.quote(owner)} {shlex.quote(target_so)}", use_sudo=True)
        self._wait_for_remote_path(remote, target_so, path_type="f", description=f"KKT driver {target_so}")
        return f"Обновлено (v{artifact.version})"

    def _step_gismt_cert(self, remote: RemoteClient) -> str | None:
        artifact = self.source.gismt_cert(self.tspiot.gismt_cert_name)
        dest = posixpath.join(self.target_data_dir, self.tspiot.gismt_cert_name)
        self.logger.info("gismt cert: %s", artifact.locator)

        remote.run(f"mkdir -p {shlex.quote(self.target_data_dir)}", use_sudo=True)
        changed = self._install(remote, artifact, dest)
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

    def _install(self, remote: RemoteClient, artifact, dest: str) -> bool:
        """Установить артефакт в dest ТОЛЬКО если он изменился. True, если заменён.

        Хэш проверяется ДО загрузки: ожидаемая контрольная сумма источника
        сравнивается с sha256 уже установленного файла. Если совпали — доставки нет.
        """
        expected = self.source.expected_sha256(artifact)
        installed = self._remote_sha256(remote, dest)
        if expected and installed == expected:
            self.logger.info("Файл не изменился (sha256 совпал), загрузка не требуется: %s", dest)
            return False
        if expected is None:
            self.logger.warning(
                "Нет контрольной суммы источника для %s: файл будет доставлен без предварительной сверки.",
                artifact.locator,
            )
        elif installed is None:
            self.logger.warning(
                "Не удалось вычислить sha256 установленного файла %s "
                "(нет sha256sum/openssl или файл отсутствует) — файл будет доставлен.",
                dest,
            )

        tmp = dest + ".dbrepair.new"
        try:
            self.source.deliver(remote, artifact, tmp)
            actual = self._remote_sha256(remote, tmp)
            if expected and actual and actual != expected:
                raise TsPiotError(
                    f"Контрольная сумма не совпала после доставки {artifact.locator} "
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
