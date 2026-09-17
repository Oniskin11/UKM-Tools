"""Публикация дистрибутивов ТС ПИоТ на веб-сервер (по SSH/SFTP).

Принимает драйвер ККТ в виде zip или папки в любой раскладке (находит
libsp-kkt-driver-x64.so / libsp-kkt-driver-x32.so), определяет версию,
заливает в UKM/kkt/<версия>/{x64,x86}/ и кладёт рядом .sha256.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
from datetime import datetime, timezone
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
    tspiot_hashes: dict[str, str] | None = None
    gismt_cert_hash: str | None = None


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


def _architecture(path_name: str, payload: bytes | None = None) -> str | None:
    """Архитектура по пути, а для ELF — по заголовку; имя самого файла не важно."""
    name = path_name.lower().replace("\\", "/")
    if re.search(r"(?:^|/)(?:x64|amd64|x86_64)(?:/|$)", name):
        return "x64"
    if re.search(r"(?:^|/)(?:x86|x32|i[3-6]86)(?:/|$)", name):
        return "x86"
    if payload and payload[:4] == b"\x7fELF" and len(payload) >= 5:
        return "x64" if payload[4] == 2 else "x86" if payload[4] == 1 else None
    return None


def collect_tspiot_binaries(source: Path, workdir: Path) -> dict[str, Path]:
    """Найти tspiot в ZIP/папке; распознаёт путь и ELF, не требуя фиксированной раскладки."""
    result: dict[str, Path] = {}

    def add(name: str, payload: bytes, suffix: str) -> None:
        filename = name.replace("\\", "/").split("/")[-1].lower()
        if not filename.startswith("tspiot"):
            return
        if filename.endswith((".txt", ".md", ".sha256", ".sig")):
            return
        arch = _architecture(name, payload)
        if arch is None:
            raise PublishError(f"Не удалось определить архитектуру tspiot: {name}")
        out = workdir / f"tspiot-{arch}-{suffix}"
        out.write_bytes(payload)
        result[arch] = out

    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for index, entry in enumerate(archive.namelist()):
                if entry.endswith("/"):
                    continue
                filename = entry.replace("\\", "/").split("/")[-1].lower()
                if filename.startswith("tspiot"):
                    add(entry, archive.read(entry), str(index))
    elif source.is_dir():
        for index, path in enumerate(sorted(p for p in source.rglob("*") if p.is_file())):
            if path.name.lower().startswith("tspiot"):
                add(str(path.relative_to(source)), path.read_bytes(), str(index))
    else:
        raise PublishError(f"Источник должен быть .zip или папкой: {source}")
    return result


def collect_gismt_cert(source: Path, workdir: Path) -> Path | None:
    """Найти `gismt_cert.txt` в ZIP или папке и подготовить его к публикации."""
    filename = "gismt_cert.txt"
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for entry in archive.namelist():
                if entry.endswith("/") or entry.replace("\\", "/").split("/")[-1].lower() != filename:
                    continue
                target = workdir / filename
                with archive.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                return target
    elif source.is_dir():
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            if path.name.lower() == filename:
                return path
    else:
        raise PublishError(f"Источник должен быть .zip или папкой: {source}")
    return None


def _publish_latest(remote: RemoteClient, publish: PublishConfig, kind: str, version: str) -> None:
    base = posixpath.join(publish.ukm_dir, kind)
    target = posixpath.join(base, "latest.json")
    temporary = target + ".dbrepair.new"
    remote.write_text(temporary, json.dumps({"version": version}) + "\n")
    remote.run(f"chown {shlex.quote(publish.owner)} {shlex.quote(temporary)} && chmod 644 {shlex.quote(temporary)}")
    remote.run(f"mv -f {shlex.quote(temporary)} {shlex.quote(target)}")


def _atomic_write(path: Path, data: bytes) -> None:
    """Записать файл целиком, не оставляя читателю промежуточную версию."""
    temporary = path.with_name(path.name + ".dbrepair.new")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def publish_distribution_local(root: Path, source: Path, logger, *, version: str | None = None) -> PublishResult:
    """Разложить дистрибутив в локальный каталог в том же формате, что и на вебе."""
    root = Path(root)
    source = Path(source)
    version = (version or detect_version(source.name) or datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M%S")).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)+", version):
        raise PublishError("Версия должна состоять из чисел, разделённых точками.")

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        try:
            drivers = collect_kkt_drivers(source, workdir)
        except PublishError:
            drivers = {}
        tspiot = collect_tspiot_binaries(source, workdir)
        gismt_cert = collect_gismt_cert(source, workdir)
        if not drivers and not tspiot and gismt_cert is None:
            raise PublishError("Не найдены драйвер ККТ, бинарник tspiot или gismt_cert.txt.")

        hashes: dict[str, str] = {}
        tspiot_hashes: dict[str, str] = {}
        gismt_cert_hash: str | None = None
        for kind, files, output in (("kkt", drivers, hashes), ("tspiot", tspiot, tspiot_hashes)):
            if not files:
                continue
            for arch, path in sorted(files.items()):
                target_dir = root / kind / version / arch
                target_dir.mkdir(parents=True, exist_ok=True)
                filename = path.name if kind == "kkt" else "tspiot"
                target = target_dir / filename
                _atomic_write(target, path.read_bytes())
                digest = _sha256(path)
                _atomic_write(target.with_name(target.name + ".sha256"), (digest + "\n").encode("ascii"))
                output[arch] = digest
                logger.info("published %s %s -> %s (sha256 %s)", version, arch, target, digest)
            _atomic_write(root / kind / "latest.json", (json.dumps({"version": version}) + "\n").encode("utf-8"))
            logger.info("published latest %s version pointer: %s", kind, version)

        if gismt_cert is not None:
            target = root / "gismt_cert.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, gismt_cert.read_bytes())
            gismt_cert_hash = _sha256(gismt_cert)
            _atomic_write(target.with_name(target.name + ".sha256"), (gismt_cert_hash + "\n").encode("ascii"))
            logger.info("published gismt certificate -> %s (sha256 %s)", target, gismt_cert_hash)

    return PublishResult(version=version, hashes=hashes, tspiot_hashes=tspiot_hashes, gismt_cert_hash=gismt_cert_hash)


def publish_distribution(publish: PublishConfig, source: Path, logger, *, version: str | None = None) -> PublishResult:
    """Опубликовать все распознанные ККТ- и ТС ПИоТ-файлы из одного источника."""
    source = Path(source)
    version = (version or detect_version(source.name) or datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M%S")).strip()
    connection = ConnectionConfig(host=publish.host, username=publish.username, password=publish.password, port=publish.port)
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        try:
            drivers = collect_kkt_drivers(source, workdir)
        except PublishError:
            drivers = {}
        tspiot = collect_tspiot_binaries(source, workdir)
        gismt_cert = collect_gismt_cert(source, workdir)
        if not drivers and not tspiot and gismt_cert is None:
            raise PublishError("Не найдены драйвер ККТ, бинарник tspiot или gismt_cert.txt.")
        hashes: dict[str, str] = {}
        tspiot_hashes: dict[str, str] = {}
        gismt_cert_hash: str | None = None
        with RemoteClient(connection, logger) as remote:
            for kind, files, output in (("kkt", drivers, hashes), ("tspiot", tspiot, tspiot_hashes)):
                if not files:
                    continue
                for arch, path in sorted(files.items()):
                    remote_dir = posixpath.join(publish.ukm_dir, kind, version, arch)
                    remote.run(f"mkdir -p {shlex.quote(remote_dir)}")
                    filename = path.name if kind == "kkt" else "tspiot"
                    remote_file = posixpath.join(remote_dir, filename)
                    remote.upload(path, remote_file)
                    digest = _sha256(path)
                    remote.write_text(remote_file + ".sha256", digest + "\n")
                    output[arch] = digest
                version_dir = posixpath.join(publish.ukm_dir, kind, version)
                remote.run(f"chown -R {shlex.quote(publish.owner)} {shlex.quote(version_dir)}", check=False)
                remote.run(f"find {shlex.quote(version_dir)} -type d -exec chmod 755 {{}} +", check=False)
                remote.run(f"find {shlex.quote(version_dir)} -type f -exec chmod 644 {{}} +", check=False)
                _publish_latest(remote, publish, kind, version)
                logger.info("published latest %s version pointer: %s", kind, version)
            if gismt_cert is not None:
                remote_cert = posixpath.join(publish.ukm_dir, "gismt_cert.txt")
                remote.upload(gismt_cert, remote_cert)
                gismt_cert_hash = _sha256(gismt_cert)
                remote.write_text(remote_cert + ".sha256", gismt_cert_hash + "\n")
                remote.run(f"chown {shlex.quote(publish.owner)} {shlex.quote(remote_cert)}* && chmod 644 {shlex.quote(remote_cert)}*")
                logger.info("published gismt certificate -> %s (sha256 %s)", remote_cert, gismt_cert_hash)
    return PublishResult(version=version, hashes=hashes, tspiot_hashes=tspiot_hashes, gismt_cert_hash=gismt_cert_hash)


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

            # Указатель обновляется последним: касса не увидит новую версию,
            # пока не загружены оба файла и их контрольные суммы.
            latest_path = posixpath.join(publish.ukm_dir, "kkt", "latest.json")
            latest_tmp_path = latest_path + ".dbrepair.new"
            latest_content = json.dumps({"version": version}, ensure_ascii=False) + "\n"
            remote.write_text(latest_tmp_path, latest_content)
            remote.run(f"mv -f {shlex.quote(latest_tmp_path)} {shlex.quote(latest_path)}")
            remote.run(
                f"chown {shlex.quote(publish.owner)} {shlex.quote(latest_path)} && "
                f"chmod 644 {shlex.quote(latest_path)}",
                check=False,
            )
            logger.info("published latest KKT version pointer: %s", version)

    return PublishResult(version=version, hashes=hashes)
