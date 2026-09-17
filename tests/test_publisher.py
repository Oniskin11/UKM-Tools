import zipfile
from pathlib import Path

import pytest

from dbrepair.publisher import PublishError, collect_kkt_drivers, collect_tspiot_binaries, detect_version, publish_distribution, publish_distribution_local
from dbrepair.distsource import LocalDistSource
from dbrepair.config import PublishConfig
import dbrepair.publisher as publisher


def test_detect_version():
    assert detect_version("Driver KKT 1.5.18.209. For ESP.zip") == "1.5.18.209"
    assert detect_version("1.5.19.211_linux") == "1.5.19.211"
    assert detect_version("driver.zip") is None


def test_collect_from_folder(tmp_path):
    root = tmp_path / "1.5.19.211_linux"
    (root / "linux" / "x64" / "shared").mkdir(parents=True)
    (root / "linux" / "x32" / "shared").mkdir(parents=True)
    (root / "linux" / "x64" / "shared" / "libsp-kkt-driver-x64.so").write_bytes(b"64")
    (root / "linux" / "x32" / "shared" / "libsp-kkt-driver-x32.so").write_bytes(b"32")

    drivers = collect_kkt_drivers(root, tmp_path / "work")
    assert set(drivers) == {"x64", "x86"}
    assert drivers["x64"].read_bytes() == b"64"
    assert drivers["x86"].name == "libsp-kkt-driver-x32.so"


def test_collect_from_zip(tmp_path):
    zip_path = tmp_path / "Driver KKT 1.5.18.209. For ESP.zip"
    work = tmp_path / "work"
    work.mkdir()
    with zipfile.ZipFile(zip_path, "w") as archive:
        # записи с обратными слэшами, как в реальном архиве
        archive.writestr("linux\\x64\\libsp-kkt-driver-x64.so", b"64")
        archive.writestr("linux\\x32\\libsp-kkt-driver-x32.so", b"32")
        archive.writestr("linux\\x64\\sp-kkt-driver.jar", b"jar")

    drivers = collect_kkt_drivers(zip_path, work)
    assert set(drivers) == {"x64", "x86"}
    assert drivers["x64"].read_bytes() == b"64"


def test_collect_missing(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(PublishError):
        collect_kkt_drivers(tmp_path / "empty", tmp_path / "work")


def test_collect_tspiot_from_zip_detects_elf_architecture(tmp_path):
    zip_path = tmp_path / "developer-drop.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("package/bin/tspiot", b"\x7fELF\x02" + b"x64")
        archive.writestr("other/tspiot.bin", b"\x7fELF\x01" + b"x86")

    work = tmp_path / "work"
    work.mkdir()
    binaries = collect_tspiot_binaries(zip_path, work)

    assert set(binaries) == {"x64", "x86"}
    assert binaries["x64"].read_bytes().startswith(b"\x7fELF\x02")


def test_collect_tspiot_from_folder_uses_architecture_in_path(tmp_path):
    binary = tmp_path / "release" / "x64" / "tspiot"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"not-an-elf")

    work = tmp_path / "work"
    work.mkdir()
    binaries = collect_tspiot_binaries(tmp_path / "release", work)

    assert set(binaries) == {"x64"}


def test_collect_tspiot_ignores_documentation(tmp_path):
    source = tmp_path / "release"
    binary = source / "x64" / "tspiot"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    (source / "x64" / "tspiot.txt").write_text("notes", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    assert set(collect_tspiot_binaries(source, work)) == {"x64"}


def test_publish_distribution_to_local_directory_creates_web_layout(tmp_path):
    source = tmp_path / "Developer package 1.0.0.0.512"
    (source / "x64").mkdir(parents=True)
    (source / "x86").mkdir()
    (source / "x64" / "libsp-kkt-driver-x64.so").write_bytes(b"driver64")
    (source / "x86" / "tspiot").write_bytes(b"tspiot32")
    (source / "gismt_cert.txt").write_bytes(b"certificate")

    class Logger:
        def info(self, *args):
            pass

    root = tmp_path / "UKM"
    result = publish_distribution_local(root, source, Logger())

    assert result.version == "1.0.0.0.512"
    assert (root / "kkt" / result.version / "x64" / "libsp-kkt-driver-x64.so").read_bytes() == b"driver64"
    assert (root / "tspiot" / result.version / "x86" / "tspiot").read_bytes() == b"tspiot32"
    assert (root / "kkt" / "latest.json").read_text(encoding="utf-8") == '{"version": "1.0.0.0.512"}\n'
    assert (root / "tspiot" / "latest.json").is_file()
    assert (root / "kkt" / result.version / "x64" / "libsp-kkt-driver-x64.so.sha256").is_file()
    assert (root / "gismt_cert.txt").read_bytes() == b"certificate"
    assert (root / "gismt_cert.txt.sha256").is_file()
    assert result.gismt_cert_hash is not None
    assert LocalDistSource(root).kkt_driver("x64").version == result.version
    assert LocalDistSource(root).tspiot("x86").filename == "tspiot"


def test_publish_certificate_only_to_web_root(tmp_path, monkeypatch):
    source = tmp_path / "certificate.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("package/gismt_cert.txt", b"certificate")

    class Logger:
        def info(self, *args):
            pass

    class Remote:
        uploads: list[tuple[str, str]] = []
        writes: list[tuple[str, str]] = []

        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def upload(self, source_path, destination):
            self.uploads.append((Path(source_path).name, destination))

        def write_text(self, path, text):
            self.writes.append((path, text))

        def run(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(publisher, "RemoteClient", Remote)
    result = publish_distribution(PublishConfig(host="web", username="root", ukm_dir="/var/www/files/UKM"), source, Logger())
    assert result.gismt_cert_hash is not None
    assert Remote.uploads == [("gismt_cert.txt", "/var/www/files/UKM/gismt_cert.txt")]
    assert Remote.writes[0][0] == "/var/www/files/UKM/gismt_cert.txt.sha256"
