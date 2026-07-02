"""Публикация дистрибутивов ТС ПИоТ на веб-сервер (по SSH/SFTP).

Принимает драйвер ККТ в виде zip или папки в любой раскладке (находит
libsp-kkt-driver-x64.so / libsp-kkt-driver-x32.so), определяет версию,
заливает в UKM/kkt/<версия>/{x64,x86}/ и кладёт рядом .sha256.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import posixpath
import re
import shlex
import shutil
import tempfile
import zipfile

from .config import ConnectionConfig, PublishConfig
from .remote import RemoteClient


_VERSION = re.compile(r"(\d+(?:\.\d+)+)")
_DRIVER_RE = re.compile(r"^libsp-kkt-driver-(x64|x32)\.so$", re.IGNORECASE)


class PublishError(RuntimeError):
    """Проблема публикации (не найден файл/версия, неверный источник)."""


@dataclass(frozen=True)
class PublishResult:
    version: str
    hashes: dict[str, str]  # arch -> sha256


PublishProgress = "typing.Callable[[str, str, str], None]"  # (arch, version, sha256)


def detect_version(name: str) -> str | None:
    match = _VERSION.search(name)
    return match.group(1) if match else None


def _arch_from_filename(filename: str) -> str:
    return "x64" if "x64" in filename.lower() else "x86"


def collect_kkt_drivers(source: Path, workdir: Path) -> dict[str, Path]:
    """Собрать {arch: путь к .so} из zip или папки (zip распаковывается в workdir)."""
    result: dict[str, Path] = {}
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for entry in archive.namelist():
                filename = entry.replace("\\", "/").split("/")[-1]
                if _DRIVER_RE.match(filename):
                    arch = _arch_from_filename(filename)
                    out = workdir / filename
                    with archive.open(entry) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    result[arch] = out
    elif source.is_dir():
        for path in sorted(source.rglob("*.so")):
            if _DRIVER_RE.match(path.name):
                result[_arch_from_filename(path.name)] = path
    else:
        raise PublishError(f"Источник должен быть .zip или папкой: {source}")

    if not result:
        raise PublishError("В источнике не найдено libsp-kkt-driver-x64.so / libsp-kkt-driver-x32.so.")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_kkt(
    publish: PublishConfig,
    source: Path,
    logger,
    *,
    version: str | None = None,
    progress=None,
) -> PublishResult:
    """Опубликовать драйвер ККТ на веб-сервер. Возвращает версию и хэши по архитектурам."""
    source = Path(source)
    version = (version or detect_version(source.name) or "").strip()
    if not version:
        raise PublishError(
            "Не удалось определить версию из имени источника — укажите версию вручную."
        )

    connection = ConnectionConfig(
        host=publish.host,
        username=publish.username,
        password=publish.password,
        port=publish.port,
    )

    hashes: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        drivers = collect_kkt_drivers(source, Path(tmpdir))
        logger.info("Publishing KKT driver v%s: arch=%s", version, ", ".join(sorted(drivers)))
        with RemoteClient(connection, logger) as remote:
            for arch, path in sorted(drivers.items()):
                remote_dir = posixpath.join(publish.ukm_dir, "kkt", version, arch)
                remote.run(f"mkdir -p {shlex.quote(remote_dir)}")
                remote_file = posixpath.join(remote_dir, path.name)
                remote.upload(path, remote_file)
                digest = _sha256(path)
                remote.write_text(remote_file + ".sha256", digest + "\n")
                hashes[arch] = digest
                logger.info("published %s %s -> %s (sha256 %s)", version, arch, remote_file, digest)
                if progress is not None:
                    progress(arch, version, digest)

            version_dir = posixpath.join(publish.ukm_dir, "kkt", version)
            remote.run(f"chown -R {shlex.quote(publish.owner)} {shlex.quote(version_dir)}", check=False)
            remote.run(f"find {shlex.quote(version_dir)} -type d -exec chmod 755 {{}} +", check=False)
            remote.run(f"find {shlex.quote(version_dir)} -type f -exec chmod 644 {{}} +", check=False)

    return PublishResult(version=version, hashes=hashes)
