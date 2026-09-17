from __future__ import annotations

from pathlib import Path
import unittest

from dbrepair.config import AppConfig, ConnectionConfig, DatabaseConfig, ServiceCommands, WorkflowPaths
from dbrepair.mysql_rebuild import MYSQL_REBUILD_STEPS, MysqlRebuildWorkflow
from dbrepair.workflow import WorkflowError


class Logger:
    def info(self, *args) -> None:
        pass


class MysqlRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            connection=ConnectionConfig(host="192.168.0.10", username="root", password="secret"),
            database=DatabaseConfig(name="ukmclient", password="secret"),
            paths=WorkflowPaths(Path("repair.tgz"), Path("empty.tgz"), Path("backups")),
            services=ServiceCommands(),
            source_path=Path("config.toml"),
        )
        self.workflow = MysqlRebuildWorkflow(self.config, Logger())

    def test_rejects_unknown_or_empty_plan(self) -> None:
        with self.assertRaises(WorkflowError):
            self.workflow.run_steps([])
        with self.assertRaises(WorkflowError):
            self.workflow.run_steps(["unknown"])

    def test_rebuild_is_split_into_observable_safe_phases(self) -> None:
        class Remote:
            def __init__(self) -> None:
                self.commands: list[tuple[str, dict]] = []
                self.written: list[tuple[str, str]] = []

            def run(self, command: str, **kwargs) -> None:
                self.commands.append((command, kwargs))

            def write_text(self, path: str, content: str) -> None:
                self.written.append((path, content))

        remote = Remote()
        self.workflow._rebuild(remote)
        commands = [command for command, _kwargs in remote.commands]
        self.assertGreaterEqual(len(commands), 12)
        self.assertIn('wget -q --timeout=30 --tries=2', commands[0])
        self.assertIn("find \"$WORK\" -type d -path '*/usr/local/mysql*/var' -print | sed -n '1p'", commands[0])
        self.assertNotIn("-print -quit", commands[0])
        self.assertNotIn('. "$RC"', commands[0])
        self.assertEqual('/etc/init.d/ukmclient stop', commands[1])
        self.assertEqual('/etc/init.d/mysql stop', commands[2])
        self.assertIn('mv "$VAR_DIR" "$BACKUP"', commands[3])
        self.assertEqual('/etc/init.d/mysql start', commands[4])
        self.assertIn('mysql "$DB_NAME" < "$SCHEMA_DUMP"', commands[6])
        self.assertIn('mysql "$DB_NAME" < "$VERSION_DUMP"', commands[7])
        self.assertIn("SHOW TABLES LIKE 'trm_in_store'", commands[8])
        self.assertIn("GRANT ALL ON *.* TO root@'localhost'", remote.written[0][1])
        self.assertIn('TERM=linux /etc/init.d/ukmclient start', commands[-2])
        self.assertIn('rm -rf /tmp/dbrepair-mysql-rebuild-', commands[-1])

    def test_phase_timeout_names_the_stuck_phase(self) -> None:
        class Remote:
            def run(self, command: str, **kwargs) -> None:
                del command, kwargs
                raise TimeoutError("late response")

        with self.assertRaisesRegex(WorkflowError, "Подготовить архивы"):
            self.workflow._run_rebuild_phase(Remote(), "Подготовить архивы", "true", 10)

    def test_plan_has_preflight_rebuild_and_verify(self) -> None:
        self.assertEqual([step.step_id for step in MYSQL_REBUILD_STEPS], ["preflight", "rebuild", "verify"])

    def test_verify_uses_a_private_client_file(self) -> None:
        class Remote:
            def __init__(self) -> None:
                self.written: list[tuple[str, str]] = []
                self.commands: list[tuple[str, dict]] = []

            def write_text(self, path: str, content: str) -> None:
                self.written.append((path, content))

            def run(self, command: str, **kwargs) -> None:
                self.commands.append((command, kwargs))

        remote = Remote()
        self.workflow._verify_with_database_password(remote)
        path, client_config = remote.written[0]
        self.assertIn('password="secret"', client_config)
        self.assertIn(f"chmod 600 {path}", remote.commands[0][0])
        self.assertIn(f"--defaults-extra-file={path}", remote.commands[1][0])
        self.assertIn("SHOW TABLES LIKE 'trm_in_store'", remote.commands[1][0])
        self.assertIn("TERM=linux /etc/init.d/ukmclient start", remote.commands[1][0])
        self.assertIn("pgrep -f '/usr/local/ukmclient/ukmstart.sh'", remote.commands[1][0])
        self.assertTrue(remote.commands[1][1]["get_pty"])
        self.assertNotIn(self.config.database.password, remote.commands[1][0])
        self.assertIn(f"rm -f {path}", remote.commands[2][0])

    def test_preflight_command_is_a_complete_shell_script(self) -> None:
        command = self.workflow._preflight_command()
        self.assertTrue(command.rstrip().endswith('echo Source: http://$server/ukminstall'))
        self.assertIn('fail() { echo "Preflight failed: $1" >&2; exit 1; }', command)
        self.assertIn('cannot download ukm-root.tar.gz from $server', command)
        self.assertIn('ukmcli-build.tgz does not contain ukm.sql', command)
        self.assertIn("awk -F=", command)
        self.assertIn("netstat -tn", command)
        self.assertIn('no established remote MySQL peer was found by netstat', command)
        self.assertIn('multiple remote MySQL peers found', command)
        self.assertNotIn('. "$RC"', command)

    def test_manual_source_server_takes_priority_over_netstat(self) -> None:
        workflow = MysqlRebuildWorkflow(self.config, Logger(), source_server="192.168.51.175")
        command = workflow._preflight_command()
        self.assertIn("manual_server=192.168.51.175", command)
        self.assertIn("source_method='manual input'", command)
        self.assertIn('if test -z "${server:-}" && test -r "$RC"', command)
        self.assertIn('if test -z "${server:-}"; then', command)

    def test_rejects_unsafe_manual_source_server(self) -> None:
        with self.assertRaisesRegex(ValueError, "Адрес сервера UKM"):
            MysqlRebuildWorkflow(self.config, Logger(), source_server="server; rm -rf /")

    def test_grants_are_sent_as_a_temporary_file(self) -> None:
        class Remote:
            def __init__(self) -> None:
                self.written: list[tuple[str, str]] = []
                self.commands: list[str] = []

            def write_text(self, path: str, content: str) -> None:
                self.written.append((path, content))

            def run(self, command: str, **kwargs) -> None:
                del kwargs
                self.commands.append(command)

        remote = Remote()
        self.workflow._apply_standard_grants(remote)
        self.assertIn("GRANT ALL ON *.* TO root@'localhost'", remote.written[0][1])
        self.assertNotIn(self.config.database.password, remote.commands[0])


if __name__ == "__main__":
    unittest.main()
