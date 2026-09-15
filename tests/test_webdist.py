import pytest

from dbrepair import webdist


@pytest.fixture(autouse=True)
def no_manifest_by_default(monkeypatch):
    """Тесты directory listing не должны ходить в сеть за latest.json."""
    def missing(url, timeout=20.0):
        raise webdist.WebDistError("404")

    monkeypatch.setattr(webdist, "_get_text", missing)


def test_version_key():
    assert webdist._version_key("1.5.18.209/") == (1, 5, 18, 209)
    assert webdist._version_key("1.0.0.0/") == (1, 0, 0, 0)
    assert webdist._version_key("x64/") == ()


def test_latest_version_picks_max(monkeypatch):
    monkeypatch.setattr(
        webdist,
        "_list",
        lambda url, timeout=20.0: ["1.5.16.191/", "1.5.18.209/", "readme.txt"],
    )
    assert webdist.latest_version("http://x/UKM/", "kkt/") == "1.5.18.209"


def test_latest_version_prefers_published_pointer(monkeypatch):
    def fake_get_text(url, timeout=20.0):
        if url.endswith("/kkt/latest.json"):
            return '{"version": "1.0.0.0.512"}'
        raise webdist.WebDistError("404")

    monkeypatch.setattr(webdist, "_get_text", fake_get_text)
    monkeypatch.setattr(webdist, "_list", lambda url, timeout=20.0: ["1.5.19.211/"])
    assert webdist.latest_version("http://x/UKM/", "kkt/") == "1.0.0.0.512"


def test_latest_version_falls_back_when_pointer_missing(monkeypatch):
    def fake_get_text(url, timeout=20.0):
        raise webdist.WebDistError("404")

    monkeypatch.setattr(webdist, "_get_text", fake_get_text)
    monkeypatch.setattr(webdist, "_list", lambda url, timeout=20.0: ["1.5.19.211/"])
    assert webdist.latest_version("http://x/UKM/", "kkt/") == "1.5.19.211"


def test_latest_version_raises_when_empty(monkeypatch):
    monkeypatch.setattr(webdist, "_list", lambda url, timeout=20.0: ["readme.txt"])
    try:
        webdist.latest_version("http://x/UKM/", "kkt/")
    except webdist.WebDistError:
        return
    raise AssertionError("expected WebDistError")


def test_tspiot_url(monkeypatch):
    monkeypatch.setattr(webdist, "_list", lambda url, timeout=20.0: ["1.0.0.0/"])
    url, version = webdist.tspiot_url("http://x/UKM/", "x86")
    assert version == "1.0.0.0"
    assert url == "http://x/UKM/tspiot/1.0.0.0/x86/tspiot"


def test_tspiot_url_uses_published_pointer(monkeypatch):
    monkeypatch.setattr(
        webdist,
        "_get_text",
        lambda url, timeout=20.0: '{"version":"2.0.0.0"}' if url.endswith("/tspiot/latest.json") else (_ for _ in ()).throw(webdist.WebDistError("404")),
    )
    url, version = webdist.tspiot_url("http://x/UKM/", "x64")
    assert version == "2.0.0.0"
    assert url == "http://x/UKM/tspiot/2.0.0.0/x64/tspiot"


def test_kkt_driver_url(monkeypatch):
    calls = {"n": 0}

    def fake_list(url, timeout=20.0):
        calls["n"] += 1
        if url.endswith("/kkt/"):
            return ["1.5.16.191/", "1.5.18.209/"]
        return ["libsp-kkt-driver-x32.so", "libsp-kkt-driver-x32.so.sha256"]

    monkeypatch.setattr(webdist, "_list", fake_list)
    url, version, filename = webdist.kkt_driver_url("http://x/UKM/", "x86")
    assert version == "1.5.18.209"
    assert filename == "libsp-kkt-driver-x32.so"
    assert url.endswith("/kkt/1.5.18.209/x86/libsp-kkt-driver-x32.so")


def test_fetch_sha256(monkeypatch):
    monkeypatch.setattr(webdist, "_get_text", lambda url, timeout=20.0: "ABC123  tspiot\n")
    assert webdist.fetch_sha256("http://x/f") == "abc123"


def test_fetch_sha256_missing(monkeypatch):
    def boom(url, timeout=20.0):
        raise webdist.WebDistError("404")

    monkeypatch.setattr(webdist, "_get_text", boom)
    assert webdist.fetch_sha256("http://x/f") is None
