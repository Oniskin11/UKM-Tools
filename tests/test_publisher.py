import zipfile

import pytest

from dbrepair.publisher import PublishError, collect_kkt_drivers, collect_tspiot_binaries, detect_version


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
