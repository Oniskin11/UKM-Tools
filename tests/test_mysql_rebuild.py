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
        self.assertGreaterEqual(len(commands), 15)
        self.assertIn('wget -q --timeout=30 --tries=2', commands[0])
        self.assertEqual(remote.commands[0][1]["timeout"], 300)
        self.assertIn("tar xzf ukmcli-build.tgz", commands[1])
        self.assertNotIn("tar tzf", commands[1])
        self.assertEqual(remote.commands[1][1]["timeout"], 900)
        self.assertIn("find \"$WORK\" -type d -path '*/usr/local/mysql*/var' -print | sed -n '1p'", commands[2])
        self.assertEqual(remote.commands[2][1]["timeout"], 90)
        self.assertNotIn("-print -quit", commands[2])
        self.assertNotIn('. "$RC"', commands[0])
        self.assertEqual('/etc/init.d/ukmclient stop', commands[3])
        self.assertEqual('/etc/init.d/mysql stop', commands[4])
        self.assertIn('mv "$VAR_DIR" "$BACKUP"', commands[5])
        self.assertEqual('/etc/init.d/mysql start', commands[6])
        self.assertEqual(remote.commands[6][1]["timeout"], 300)
        self.assertIn('_mysql_ready_command', MysqlRebuildWorkflow._rebuild.__code__.co_names)
        self.assertIn('mysql "$DB_NAME" < "$SCHEMA_DUMP"', commands[8])
        self.assertIn('CHARACTER SET utf8 COLLATE utf8_general_ci', commands[8])
        self.assertIn('incoming Cyrillic data would be converted to question marks', commands[8])
        self.assertNotIn('CHARACTER SET latin1', '\n'.join(commands))
        self.assertIn('mysql "$DB_NAME" < "$VERSION_DUMP"', commands[9])
        self.assertIn("SHOW TABLES LIKE 'trm_in_store'", commands[10])
        self.assertIn('UKM database charset must be utf8', commands[10])
        self.assertIn('UKM cashier names must use utf8', commands[10])
        self.assertIn("GRANT ALL PRIVILEGES ON \\`$DB_NAME\\`.*", commands[11])
        self.assertIn('read_rc_value() { grep "^$1=" "$RC"', commands[11])
        self.assertNotIn(self.config.database.password, commands[11])
        self.assertIn('TERM=linux /etc/init.d/ukmclient start', commands[12])
        self.assertIn('mysql --defaults-extra-file="$CLIENT_FILE" -D "$DB_NAME"', commands[13])
        self.assertIn('pidof cashmain', commands[13])
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
        self.assertIn("pidof cashmain", remote.commands[1][0])
        self.assertTrue(remote.commands[1][1]["get_pty"])
        self.assertNotIn(self.config.database.password, remote.commands[1][0])
        self.assertIn(f"rm -f {path}", remote.commands[2][0])

    def test_preflight_command_is_a_complete_shell_script(self) -> None:
        command = self.workflow._preflight_command()
        self.assertTrue(command.rstrip().endswith('echo Source: http://$server/ukminstall'))
        self.assertIn('fail() { echo "Preflight failed: $1" >&2; exit 1; }', command)
        self.assertIn('cannot download ukm-root.tar.gz from $server', command)
        self.assertIn('ukmcli-build.tgz does not contain ukm.sql', command)
        self.assertIn('mysqld is waiting for disk I/O (D state)', command)
        self.assertIn("ps -o stat= -C mysqld", command)
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

    def test_restore_account_uses_rc_ukm_and_scoped_privileges(self) -> None:
        command = self.workflow._restore_ukm_database_account_command()
        self.assertIn('RC=/usr/local/ukmclient/rc.ukm', command)
        self.assertIn('read_rc_value() { grep "^$1=" "$RC"', command)
        self.assertIn("GRANT ALL PRIVILEGES ON \\`$DB_NAME\\`.*", command)
        self.assertIn('WITH GRANT OPTION', command)
        self.assertIn('GRANT SELECT, INSERT, UPDATE, DELETE ON mysql.*', command)
        self.assertIn('GRANT RELOAD ON *.*', command)
        self.assertNotIn('DELETE FROM mysql.', command)
        self.assertNotIn(self.config.database.password, command)

    def test_verify_account_uses_private_file_and_cashmain(self) -> None:
        command = self.workflow._verify_ukm_database_account_command()
        self.assertIn('mktemp /tmp/.dbrepair-ukm-client.XXXXXX', command)
        self.assertIn('mysql --defaults-extra-file="$CLIENT_FILE" -D "$DB_NAME"', command)
        self.assertIn("SHOW TABLES LIKE 'trm_in_store'", command)
        self.assertIn('SELECT User FROM mysql.db', command)
        self.assertIn('pidof cashmain', command)
        self.assertNotIn(self.config.database.password, command)


if __name__ == "__main__":
    unittest.main()
