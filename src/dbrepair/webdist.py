"""Определение последних версий дистрибутивов по HTTP-листингу веб-сервера.

Ожидаемая структура на сервере (см. ukm-publish.py):
    <base>/tspiot/<версия>/<x64|x86>/tspiot
    <base>/kkt/<версия>/<x64|x86>/libsp-kkt-driver-*.so
    <base>/gismt_cert.txt
"""

from __future__ import annotations

import re
import json
import urllib.parse
import urllib.request


class WebDistError(RuntimeError):
    """Поднимается, когда нужный файл/версия не найдены на веб-сервере."""


_VERSION = re.compile(r"(\d+(?:\.\d+)+)")
_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def _get_text(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "dbrepair"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise WebDistError(f"Не удалось получить {url}: {exc}") from exc


def _list(url: str, timeout: float = 20.0) -> list[str]:
    html = _get_text(url, timeout=timeout)
    names: list[str] = []
    for href in _HREF.findall(html):
        if href.startswith(("?", "/", "http://", "https://")):
            continue
        names.append(urllib.parse.unquote(href))
    return names


def fetch_sha256(file_url: str, timeout: float = 20.0) -> str | None:
    """Получить ожидаемый sha256 рядом с файлом (<url>.sha256). None, если нет."""
    try:
        data = _get_text(file_url + ".sha256", timeout=timeout)
    except WebDistError:
        return None
    parts = data.strip().split()
    return parts[0].lower() if parts else None


def _version_key(name: str) -> tuple[int, ...]:
    match = _VERSION.search(name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def latest_version(base_url: str, sub: str) -> str:
    manifest_version = _latest_version_from_manifest(base_url, sub)
    if manifest_version is not None:
        return manifest_version

    url = urllib.parse.urljoin(base_url, sub)
    versioned = [
        (key, name.rstrip("/"))
        for name in _list(url)
        if name.endswith("/") and (key := _version_key(name))
    ]
    if not versioned:
        raise WebDistError(f"Не найдено ни одной версии в {url}")
    versioned.sort()
    return versioned[-1][1]


def _latest_version_from_manifest(base_url: str, sub: str) -> str | None:
    """Версия из <sub>/latest.json, если указатель опубликован.

    Указатель нужен для версий, которые нельзя корректно сопоставить простым
    сравнением чисел в имени каталога. Отсутствие файла сохраняет совместимость
    со старыми HTTP-каталогами: тогда используется directory listing.
    """
    manifest_url = urllib.parse.urljoin(base_url, f"{sub.rstrip('/')}/latest.json")
    try:
        payload = json.loads(_get_text(manifest_url))
    except WebDistError:
        return None
    except json.JSONDecodeError as exc:
        raise WebDistError(f"Некорректный указатель последней версии: {manifest_url}") from exc

    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip() or not _version_key(version):
        raise WebDistError(f"Некорректная версия в указателе: {manifest_url}")
    return version.strip().rstrip("/")


def tspiot_url(base_url: str, architecture: str) -> tuple[str, str]:
    """Вернуть (url бинарника tspiot, версия) для архитектуры."""
    version = latest_version(base_url, "tspiot/")
    url = urllib.parse.urljoin(base_url, f"tspiot/{version}/{architecture}/tspiot")
    return url, version


def kkt_driver_url(base_url: str, architecture: str) -> tuple[str, str, str]:
    """Вернуть (url файла libsp-kkt-driver-*.so, версия, имя файла)."""
    version = latest_version(base_url, "kkt/")
    arch_url = urllib.parse.urljoin(base_url, f"kkt/{version}/{architecture}/")
    candidates = [
        name
        for name in _list(arch_url)
        if name.startswith("libsp-kkt-driver-") and name.endswith(".so")
    ]
    if not candidates:
        raise WebDistError(f"Не найден libsp-kkt-driver-*.so в {arch_url}")
    filename = candidates[0]
    return urllib.parse.urljoin(arch_url, urllib.parse.quote(filename)), version, filename


def gismt_cert_url(base_url: str, filename: str = "gismt_cert.txt") -> str:
    return urllib.parse.urljoin(base_url, urllib.parse.quote(filename))
