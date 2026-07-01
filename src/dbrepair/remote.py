from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import posixpath
import shlex
import threading
import time
from typing import Any, TYPE_CHECKING
import uuid

try:
    import paramiko
except ModuleNotFoundError:  # pragma: no cover - optional until runtime
    paramiko = None

if TYPE_CHECKING:  # pragma: no cover
    import paramiko as paramiko_types

from .config import ConnectionConfig


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_status: int
    stdout: str
    stderr: str


class RemoteCommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        message = (
            f"Remote command failed with exit status {result.exit_status}: {result.command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        super().__init__(message)
        self.result = result


class OperationCancelledError(RuntimeError):
    """Raised when the current workflow is cancelled by the user."""


class RemoteClient:
    def __init__(
        self,
        config: ConnectionConfig,
        logger,
        *,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.logger = logger
        self.cancel_event = cancel_event
        self._client: Any | None = None
        self._sftp: Any | None = None

    def __enter__(self) -> "RemoteClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self._client is not None:
            return
        self._raise_if_cancelled()
        if paramiko is None:
            raise RuntimeError(
                "Package 'paramiko' is required. Install dependencies with 'pip install -r requirements.txt'."
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            key_filename=str(self.config.key_filename) if self.config.key_filename else None,
            timeout=self.config.timeout,
            banner_timeout=self.config.timeout,
            auth_timeout=self.config.timeout,
            look_for_keys=self.config.key_filename is None and self.config.password is None,
        )
        self._client = client
        self._sftp = client.open_sftp()
        self.logger.info("SSH connection established to %s:%s", self.config.host, self.config.port)

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        use_sudo: bool = False,
        check: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        client = self._require_client()
        self._raise_if_cancelled()
        command_to_run = self._wrap_command(command, cwd=cwd, use_sudo=use_sudo)
        self.logger.info("RUN %s", command_to_run)

        try:
            stdin, stdout, stderr = client.exec_command(
                command_to_run,
                get_pty=bool(use_sudo and self.config.use_sudo),
                timeout=timeout,
            )

            if use_sudo and self.config.use_sudo:
                sudo_password = self.config.resolved_sudo_password()
                if sudo_password:
                    stdin.write(f"{sudo_password}\n")
                    stdin.flush()

            channel = stdout.channel
            out_chunks: list[str] = []
            err_chunks: list[str] = []
            deadline = time.monotonic() + timeout if timeout else None

            while True:
                self._raise_if_cancelled(channel)
                while channel.recv_ready():
                    out_chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
                while channel.recv_stderr_ready():
                    err_chunks.append(channel.recv_stderr(4096).decode("utf-8", errors="replace"))

                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

                if deadline is not None and time.monotonic() > deadline:
                    channel.close()
                    raise TimeoutError(f"Remote command timed out after {timeout} seconds: {command}")

                time.sleep(0.1)

            result = CommandResult(
                command=command,
                exit_status=channel.recv_exit_status(),
                stdout="".join(out_chunks),
                stderr="".join(err_chunks),
            )
        except Exception as exc:
            if self._is_cancelled():
                raise OperationCancelledError("Operation cancelled by user.") from exc
            raise

        if result.stdout.strip():
            self.logger.info("STDOUT %s", result.stdout.strip())
        if result.stderr.strip():
            self.logger.warning("STDERR %s", result.stderr.strip())

        if check and result.exit_status != 0:
            raise RemoteCommandError(result)

        return result

    def read_text(self, remote_path: str, *, use_sudo: bool = False) -> str:
        self._raise_if_cancelled()
        if use_sudo and self.config.use_sudo:
            result = self.run(f"cat {shlex.quote(remote_path)}", use_sudo=True)
            return result.stdout

        with self._require_sftp().file(remote_path, mode="r") as remote_file:
            return remote_file.read().decode("utf-8")

    def write_text(self, remote_path: str, content: str, *, use_sudo: bool = False) -> None:
        self._raise_if_cancelled()
        if use_sudo and self.config.use_sudo:
            temp_path = self._temporary_remote_file("write")
            self._write_temp_file(temp_path, content.encode("utf-8"))
            mode = self.get_mode(remote_path, default=0o644)
            try:
                self.run(
                    f"install -m {mode:o} {shlex.quote(temp_path)} {shlex.quote(remote_path)}",
                    use_sudo=True,
                )
            finally:
                self.run(f"rm -f {shlex.quote(temp_path)}", check=False)
            return

        with self._require_sftp().file(remote_path, mode="w") as remote_file:
            remote_file.write(content.encode("utf-8"))

    def upload(self, local_path: Path, remote_path: str, *, use_sudo: bool = False) -> None:
        self._raise_if_cancelled()
        self.logger.info("UPLOAD %s -> %s", local_path, remote_path)
        if use_sudo and self.config.use_sudo:
            temp_path = self._temporary_remote_file(local_path.name)
            self._require_sftp().put(str(local_path), temp_path, callback=self._transfer_callback)
            try:
                self.run(
                    f"install -m 0644 {shlex.quote(temp_path)} {shlex.quote(remote_path)}",
                    use_sudo=True,
                )
            finally:
                self.run(f"rm -f {shlex.quote(temp_path)}", check=False)
            return

        self._require_sftp().put(str(local_path), remote_path, callback=self._transfer_callback)

    def download(self, remote_path: str, local_path: Path, *, use_sudo: bool = False) -> None:
        self._raise_if_cancelled()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info("DOWNLOAD %s -> %s", remote_path, local_path)

        if use_sudo and self.config.use_sudo:
            temp_path = self._temporary_remote_file(Path(remote_path).name)
            try:
                self.run(
                    f"cp {shlex.quote(remote_path)} {shlex.quote(temp_path)}"
                    f" && chmod 0644 {shlex.quote(temp_path)}",
                    use_sudo=True,
                )
                self._require_sftp().get(temp_path, str(local_path), callback=self._transfer_callback)
            finally:
                self.run(f"rm -f {shlex.quote(temp_path)}", check=False)
            return

        self._require_sftp().get(remote_path, str(local_path), callback=self._transfer_callback)

    def exists(self, remote_path: str) -> bool:
        try:
            self._require_sftp().stat(remote_path)
            return True
        except OSError:
            return False

    def get_mode(self, remote_path: str, default: int = 0o644) -> int:
        try:
            return self._require_sftp().stat(remote_path).st_mode & 0o777
        except OSError:
            return default

    def _wrap_command(self, command: str, *, cwd: str | None, use_sudo: bool) -> str:
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        shell_command = f"/bin/sh -lc {shlex.quote(command)}"
        if use_sudo and self.config.use_sudo:
            return f"sudo -S -p '' {shell_command}"
        return shell_command

    def _temporary_remote_file(self, suffix: str) -> str:
        return posixpath.join("/tmp", f".dbrepair-{uuid.uuid4().hex}-{suffix}")

    def _write_temp_file(self, remote_path: str, payload: bytes) -> None:
        self._raise_if_cancelled()
        with self._require_sftp().file(remote_path, mode="wb") as remote_file:
            remote_file.write(payload)

    def _transfer_callback(self, transferred: int, total: int) -> None:
        del transferred, total
        self._raise_if_cancelled()

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("SSH client is not connected.")
        return self._client

    def _require_sftp(self) -> Any:
        if self._sftp is None:
            raise RuntimeError("SFTP client is not connected.")
        return self._sftp

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _raise_if_cancelled(self, channel: Any | None = None) -> None:
        if not self._is_cancelled():
            return
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        raise OperationCancelledError("Operation cancelled by user.")
