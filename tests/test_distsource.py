import hashlib

import pytest

from dbrepair.config import AppConfig, ConnectionConfig, DatabaseConfig, ServiceCommands, WorkflowPaths, DistributionConfig
from dbrepair.distsource import DistError, LocalDistSource, WebDistSource, build_source
from pathlib import Path


def _make_local_tree(root):
    (root / "tspiot" / "1.0.0.0" / "x64").mkdir(parents=True)
    (root / "tspiot" / "1.0.0.0" / "x86").mkdir(parents=True)
    (root / "tspiot" / "1.0.0.0" / "x86" / "tspiot").write_bytes(b"BINARY")
    (root / "kkt" / "1.5.16.191" / "x86").mkdir(parents=True)
    (root / "kkt" / "1.5.18.209" / "x86").mkdir(parents=True)
    (root / "kkt" / "1.5.18.209" / "x86" / "libsp-kkt-driver-x32.so").write_bytes(b"DRIVER")
    (root / "gismt_cert.txt").write_bytes(b"CERT")


def test_local_source_selects_latest_and_hashes(tmp_path):
    _make_local_tree(tmp_path)
    source = LocalDistSource(tmp_path)

    tspiot = source.tspiot("x86")
    assert tspiot.version == "1.0.0.0"
    assert tspiot.filename == "tspiot"

    kkt = source.kkt_driver("x86")
    assert kkt.version == "1.5.18.209"
    assert kkt.filename == "libsp-kkt-driver-x32.so"

    cert = source.gismt_cert("gismt_cert.txt")
    assert cert.filename == "gismt_cert.txt"

    expected = hashlib.sha256(b"DRIVER").hexdigest()
    assert source.expected_sha256(kkt) == expected


def test_local_source_missing_arch(tmp_path):
    _make_local_tree(tmp_path)
    source = LocalDistSource(tmp_path)
    with pytest.raises(DistError):
        source.tspiot("x64")  # для x64 бинарник не создан


def _config(distribution=None, webserver=None):
    return AppConfig(
        connection=ConnectionConfig(host="h", username="root", password="p"),
        database=DatabaseConfig(name="db", password="x"),
        paths=WorkflowPaths(dbrepair_archive=Path("."), empty_datadir_archive=Path("."), local_backup_dir=Path(".")),
        services=ServiceCommands(),
        source_path=Path("."),
        distribution=distribution,
        webserver=webserver,
    )


def test_build_source_prefers_local_dir(tmp_path):
    cfg = _config(distribution=DistributionConfig(base_url="http://x/", local_dir=tmp_path))
    assert isinstance(build_source(cfg), LocalDistSource)


def test_build_source_http(tmp_path):
    cfg = _config(distribution=DistributionConfig(base_url="http://x/UKM/"))
    assert isinstance(build_source(cfg), WebDistSource)


def test_build_source_not_configured():
    with pytest.raises(DistError):
        build_source(_config())
