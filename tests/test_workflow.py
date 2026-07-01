from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from dbrepair.config import AppConfig, ConnectionConfig, DatabaseConfig, ServiceCommands, WorkflowPaths
from dbrepair.workflow import DbRepairWorkflow, SUCCESS_RESTORE


class FakeLogger:
    def info(self, msg, *args) -> None:
        del msg, args

    def warning(self, msg, *args) -> None:
        del msg, args


class FakeRemote:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def run(self, command: str, **kwargs):
        self.commands.append((command, kwargs))
        stdout = ""
        if "dbrestore.sh" in command:
            stdout = SUCCESS_RESTORE
        return SimpleNamespace(stdout=stdout, stderr="", exit_status=0)


class WorkflowStopUkmclientTests(unittest.TestCase):
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
        self.workflow = DbRepairWorkflow(self.config, FakeLogger())
        self.session = self.workflow.create_session("20260308-120000-000000")

    def test_backup_step_stops_ukmclient_before_mysql(self) -> None:
        remote = FakeRemote()
        self.workflow._wait_for_ukmclient_stopped = lambda current_remote: None
        self.workflow._wait_for_mysql_stopped = lambda current_remote: None
        self.workflow._wait_for_remote_path = lambda *args, **kwargs: None

        self.workflow._execute_step("backup_mysql", remote, self.session)

        self.assertEqual(remote.commands[0][0], self.config.services.ukmclient_stop)
        self.assertEqual(remote.commands[1][0], self.config.services.mysql_stop)

    def test_restore_step_stops_ukmclient_before_restore_script(self) -> None:
        remote = FakeRemote()
        self.workflow._wait_for_ukmclient_stopped = lambda current_remote: None

        self.workflow._execute_step("restore_db", remote, self.session)

        self.assertEqual(remote.commands[0][0], self.config.services.ukmclient_stop)
        self.assertEqual(remote.commands[1][0], "chmod +x dbrestore.sh && ./dbrestore.sh")

    def test_start_step_does_not_stop_ukmclient_first(self) -> None:
        remote = FakeRemote()
        self.workflow._wait_for_ukmclient_started = lambda current_remote: None

        self.workflow._execute_step("start_ukmclient", remote, self.session)

        self.assertEqual(remote.commands[0][0], self.config.services.ukmclient_start)


if __name__ == "__main__":
    unittest.main()
