from __future__ import annotations

from pathlib import Path
import unittest

from dbrepair.config import AppConfig, ConnectionConfig, DatabaseConfig, ServiceCommands, WorkflowPaths, load_config, override_host


class OverrideHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            connection=ConnectionConfig(host="192.168.0.10", username="root", password="secret"),
            database=DatabaseConfig(name="ukmclient", password="CtHDbCGK.C"),
            paths=WorkflowPaths(
                dbrepair_archive=Path("dbrepair6700+.tgz"),
                empty_datadir_archive=Path("mysql5-datadir-empty_46+.tgz"),
                local_backup_dir=Path("backups"),
            ),
            services=ServiceCommands(),
            source_path=Path("config.toml"),
        )

    def test_keeps_original_when_override_is_blank(self) -> None:
        self.assertIs(override_host(self.config, "  "), self.config)

    def test_replaces_host_only(self) -> None:
        updated = override_host(self.config, "10.10.10.10")
        self.assertEqual(updated.connection.host, "10.10.10.10")
        self.assertEqual(updated.connection.username, self.config.connection.username)
        self.assertEqual(updated.database, self.config.database)
        self.assertNotEqual(updated.connection, self.config.connection)


def test_publish_password_reads_local_dotenv(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        """[connection]
host = "cash"
username = "root"
[database]
name = "ukmclient"
password = "db-password"
[paths]
dbrepair_archive = "repair.tgz"
empty_datadir_archive = "empty.tgz"
local_backup_dir = "backups"
[publish]
host = "web"
username = "root"
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("UKM_PUBLISH_PASSWORD=from-dotenv\n", encoding="utf-8")

    config = load_config(tmp_path / "config.toml")
    assert config.publish is not None
    assert config.publish.password == "from-dotenv"


if __name__ == "__main__":
    unittest.main()
