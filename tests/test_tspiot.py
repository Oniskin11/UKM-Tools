import logging
from pathlib import Path

from dbrepair.config import (
    AppConfig,
    ConnectionConfig,
    DatabaseConfig,
    ServiceCommands,
    TsPiotConfig,
    WorkflowPaths,
)
from dbrepair.remote import CommandResult
from dbrepair.tspiot import TsPiotInstaller, _last_line


def test_last_line_ignores_banner_noise():
    assert _last_line("Login banner\n755\n") == "755"
    assert _last_line("  ukmclient:ukmclient  ") == "ukmclient:ukmclient"
    assert _last_line("") == ""


def test_resolve_owner_from_target():
    assert TsPiotConfig().resolve_owner("/usr/local/lillo") == "lillo:lillo"
    assert TsPiotConfig(owner="root:root").resolve_owner("/usr/local/ukmclient") == "root:root"


def _make_installer():
    config = AppConfig(
        connection=ConnectionConfig(host="h", username="root", password="p"),
        database=DatabaseConfig(name="db", password="x"),
        paths=WorkflowPaths(
            dbrepair_archive=Path("."),
            empty_datadir_archive=Path("."),
            local_backup_dir=Path("."),
        ),
        services=ServiceCommands(),
        source_path=Path("."),
        tspiot=TsPiotConfig(),
    )
    return TsPiotInstaller(
        config,
        logging.getLogger("test-tspiot"),
        base_url="http://x/UKM/",
        architecture="x64",
        target_base="/usr/local/ukmclient",
    )


class _FakeRemote:
    """Отдаёт заранее заданные sha256 по очереди; фиксирует все команды."""

    def __init__(self, hashes):
        self.hashes = list(hashes)
        self.commands: list[str] = []
        self._i = 0

    def run(self, command, *, use_sudo=False, check=True, timeout=None, cwd=None):
        self.commands.append(command)
        if any(tool in command for tool in ("sha256sum", "openssl dgst", "shasum")):
            value = self.hashes[self._i] if self._i < len(self.hashes) else ""
            self._i += 1
            return CommandResult(command, 0, (value or "") + "\n", "")
        return CommandResult(command, 0, "", "")

    def _downloaded(self):
        return any(("curl" in c or "wget" in c or "mv -f" in c) for c in self.commands)


def test_skip_download_when_hash_matches():
    installer = _make_installer()
    digest = "a" * 64
    remote = _FakeRemote([digest])
    changed = installer._download_and_install(remote, url="http://x/f", dest="/d/tspiot", expected=digest)
    assert changed is False
    assert not remote._downloaded()


def test_download_when_hash_differs():
    installer = _make_installer()
    old, new = "a" * 64, "b" * 64
    # installed=old, tmp(after download)=new, expected=new
    remote = _FakeRemote([old, new])
    changed = installer._download_and_install(remote, url="http://x/f", dest="/d/tspiot", expected=new)
    assert changed is True
    assert remote._downloaded()
