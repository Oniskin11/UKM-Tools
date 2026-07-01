"""Источники дистрибутивов ТС ПИоТ: HTTP-веб-сервер или локальный каталог.

Ожидаемая структура (одинаковая для обоих источников):
    <корень>/tspiot/<версия>/<x64|x86>/tspiot            (+ tspiot.sha256 для HTTP)
    <корень>/kkt/<версия>/<x64|x86>/libsp-kkt-driver-*.so (+ .sha256 для HTTP)
    <корень>/gismt_cert.txt

- HTTP: касса скачивает файлы по curl/wget напрямую; хэш берётся из <url>.sha256.
- Локальный каталог: утилита заливает файлы на кассу по SFTP; хэш считается локально.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import shlex

from . import webdist
from .remote import RemoteClient


DOWNLOAD_TIMEOUT_SECONDS = 300
_VERSION = re.compile(r"(\d+(?:\.\d+)+)")


class DistError(RuntimeError):
    """Проблема с источником дистрибутивов (не настроен, нет файла/версии)."""


@dataclass(frozen=True)
class Artifact:
    version: str
    filename: str
    locator: str  # URL (для HTTP) или путь к локальному файлу


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


class DistSource:
    """Интерфейс источника дистрибутива."""

    def describe(self) -> str:
        raise NotImplementedError

    def tspiot(self, architecture: str) -> Artifact:
        raise NotImplementedError

    def kkt_driver(self, architecture: str) -> Artifact:
        raise NotImplementedError

    def gismt_cert(self, filename: str) -> Artifact:
        raise NotImplementedError

    def expected_sha256(self, artifact: Artifact) -> str | None:
        raise NotImplementedError

    def deliver(self, remote: RemoteClient, artifact: Artifact, dest: str) -> None:
        """Положить файл артефакта на кассу по пути dest."""
        raise NotImplementedError


class WebDistSource(DistSource):
    def __init__(self, base_url: str):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"

    def describe(self) -> str:
        return f"HTTP {self.base_url}"

    def tspiot(self, architecture: str) -> Artifact:
        url, version = webdist.tspiot_url(self.base_url, architecture)
        return Artifact(version, "tspiot", url)

    def kkt_driver(self, architecture: str) -> Artifact:
        url, version, filename = webdist.kkt_driver_url(self.base_url, architecture)
        return Artifact(version, filename, url)

    def gismt_cert(self, filename: str) -> Artifact:
        return Artifact("", filename, webdist.gismt_cert_url(self.base_url, filename))

    def expected_sha256(self, artifact: Artifact) -> str | None:
        return webdist.fetch_sha256(artifact.locator)

    def deliver(self, remote: RemoteClient, artifact: Artifact, dest: str) -> None:
        remote.run(
            _download_command(artifact.locator, dest),
            use_sudo=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )


class LocalDistSource(DistSource):
    def __init__(self, root: Path):
        self.root = Path(root)

    def describe(self) -> str:
        return f"локальный каталог {self.root}"

    def _latest_version_dir(self, sub: str) -> Path:
        base = self.root / sub
        if not base.is_dir():
            raise DistError(f"Каталог не найден: {base}")
        versioned = [(_version_key(p.name), p) for p in base.iterdir() if p.is_dir() and _version_key(p.name)]
        if not versioned:
            raise DistError(f"Нет ни одной версии в {base}")
        versioned.sort()
        return versioned[-1][1]

    def tspiot(self, architecture: str) -> Artifact:
        version_dir = self._latest_version_dir("tspiot")
        path = version_dir / architecture / "tspiot"
        if not path.is_file():
            raise DistError(f"Не найден файл tspiot: {path}")
        return Artifact(version_dir.name, "tspiot", str(path))

    def kkt_driver(self, architecture: str) -> Artifact:
        version_dir = self._latest_version_dir("kkt")
        arch_dir = version_dir / architecture
        matches = sorted(arch_dir.glob("libsp-kkt-driver-*.so"))
        if not matches:
            raise DistError(f"Не найден libsp-kkt-driver-*.so в {arch_dir}")
        path = matches[0]
        return Artifact(version_dir.name, path.name, str(path))

    def gismt_cert(self, filename: str) -> Artifact:
        path = self.root / filename
        if not path.is_file():
            raise DistError(f"Не найден файл сертификата: {path}")
        return Artifact("", filename, str(path))

    def expected_sha256(self, artifact: Artifact) -> str | None:
        path = Path(artifact.locator)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def deliver(self, remote: RemoteClient, artifact: Artifact, dest: str) -> None:
        remote.upload(Path(artifact.locator), dest, use_sudo=True)


def _version_key(name: str) -> tuple[int, ...]:
    match = _VERSION.search(name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def build_source(config) -> DistSource:
    """Построить источник по конфигу: [distribution] (local_dir | base_url) или legacy [webserver]."""
    dist = getattr(config, "distribution", None)
    if dist is not None:
        if dist.local_dir is not None:
            return LocalDistSource(dist.local_dir)
        if dist.base_url:
            return WebDistSource(dist.base_url)
    if getattr(config, "webserver", None) is not None:
        return WebDistSource(config.webserver.base_url)
    raise DistError(
        "Источник дистрибутивов не настроен: задайте [distribution] (local_dir или base_url) "
        "либо [webserver] base_url в config.toml."
    )
