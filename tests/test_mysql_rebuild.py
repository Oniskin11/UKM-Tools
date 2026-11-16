from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dbrepair.config import AppConfig, ConnectionConfig, DatabaseConfig, ServiceCommands, WorkflowPaths
from dbrepair.mysql_rebuild import MysqlRebuildWorkflow, RebuildPausedError, _mysql_ready_command, _mysql_io_guard
from dbrepair.remote import CommandResult, RemoteClient
from dbrepair.workflow import WorkflowError


class Remote:
    def __init__(self, results=()):
        self.commands = []
        self.written = []
        self.scripts = []
        self.results = iter(results)
        self.cancel_event = None

    def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if "-e 'SELECT 1'" in command:
            return next(self.results, CommandResult(command, 0, '1\n', ''))
        if command.startswith('JOB='):
            return CommandResult(command, 0, 'MYSQL_READY\n' if 'mysql-start' in command else 'JOB_EXIT=0\n', '')
        return CommandResult(command, 0, '', '')

    def write_private_text(self, path, content, **kwargs):
        self.written.append((path, content))

    def write_text(self, path, content, **kwargs):
        self.scripts.append((path, content))


class MysqlRebuildTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(
            connection=ConnectionConfig(host="192.168.0.10", username="root", password="ssh-secret"),
            database=DatabaseConfig(name="ukmclient", password="mysql-secret"),
            paths=WorkflowPaths(Path("repair.tgz"), Path("empty.tgz"), Path("backups")),
            services=ServiceCommands(), source_path=Path("config.toml"),
        )
        self.workflow = MysqlRebuildWorkflow(self.config, Mock())

    def test_invalid_inputs(self):
        for plan in ([], ['unknown']):
            with self.assertRaises(WorkflowError):
                self.workflow.run_steps(plan)
        with self.assertRaises(ValueError):
            MysqlRebuildWorkflow(replace(self.config, database=DatabaseConfig("ukm';x", "p")), Mock())
        with self.assertRaises(ValueError):
            MysqlRebuildWorkflow(self.config, Mock(), source_server='host; exit 0')

    def test_manual_source_and_trusted_archives(self):
        w = MysqlRebuildWorkflow(self.config, Mock(), source_server='192.168.117.75')
        script = w._preflight_command()
        self.assertIn('manual_server=192.168.117.75', script)
        self.assertNotIn('. "$RC"', script)
        self.assertNotIn('tar tzf', script)

    def test_rebuild_preflights_and_uses_sudo_and_one_login(self):
        remote = Remote()
        self.workflow._rebuild(remote)
        commands = [c for c, _ in remote.commands]
        self.assertTrue(any('id -u' in c for c in commands))
        self.assertTrue(all(kwargs['use_sudo'] for _, kwargs in remote.commands))
        client = remote.written[0][0]
        for marker in ('< "$SCHEMA_DUMP"', '< "$VERSION_DUMP"', 'GRANT ALL PRIVILEGES', 'DEFAULT_CHARACTER_SET_NAME'):
            self.assertIn(f'--defaults-file={client}', next(c for c in commands + [s for _,s in remote.scripts] if marker in c))
        self.assertNotIn('mysql-secret', '\n'.join(commands))
        self.assertNotIn('ssh-secret', '\n'.join(commands))
        self.assertTrue(commands[-1].startswith('rm -rf /tmp/dbrepair-mysql-rebuild-'))

    def test_verify_checks_schema_and_login_before_start(self):
        remote = Remote()
        self.workflow._verify_with_database_password(remote)
        commands = [c for c, _ in remote.commands]
        schema = next(i for i,c in enumerate(commands) if 'DEFAULT_CHARACTER_SET_NAME' in c)
        start = next(i for i,c in enumerate(commands) if '/etc/init.d/ukmclient start' in c)
        login = next(i for i,c in enumerate(commands) if 'protocol=tcp' in c)
        self.assertLess(schema, start)
        self.assertLess(login, start)
        self.assertIn('UKM cashier names must use utf8', commands[schema])

    def test_password_fallback_only_on_1045(self):
        denied = CommandResult('', 1, '', 'ERROR 1045: Access denied')
        remote = Remote([denied])
        with self.workflow._mysql_credentials(remote):
            pass
        self.assertEqual(len(remote.written), 2)
        self.assertIn('password="mysql-secret"', remote.written[0][1])
        self.assertIn('password=""', remote.written[1][1])
        self.assertIn('protocol=socket', remote.written[1][1])
        self.assertIn('rm -f', remote.commands[-1][0])
        with self.assertRaisesRegex(WorkflowError, r'\[database\].password'):
            with self.workflow._mysql_credentials(Remote([denied, denied])):
                self.fail('must not yield')
        remote = Remote([CommandResult('', 1, '', 'ERROR 2002: no socket')])
        with self.assertRaisesRegex(WorkflowError, 'локальный сокет'):
            with self.workflow._mysql_credentials(remote):
                self.fail('must not yield')
        self.assertEqual(len(remote.written), 1)

    def test_credential_escaping_and_no_retry_on_import_error(self):
        w = MysqlRebuildWorkflow(replace(self.config, database=DatabaseConfig('ukmclient', 'a\\b"c\nd')), Mock())
        remote = Remote()
        with self.assertRaisesRegex(RuntimeError, 'import failed'):
            with w._mysql_credentials(remote):
                raise RuntimeError('import failed')
        self.assertEqual(len(remote.written), 1)
        self.assertIn('password="a\\\\b\\"c\\nd"', remote.written[0][1])
        self.assertIn('rm -f', remote.commands[-1][0])

    def test_failed_rollback_preserves_workdir_and_diagnoses_mysql(self):
        remote = Remote()
        original = remote.run
        def run(command, **kwargs):
            if 'mv "$VAR_DIR" "$BACKUP"' in command:
                raise TimeoutError('disk blocked')
            return original(command, **kwargs)
        remote.run = run
        self.workflow._restore_services = Mock(return_value=False)
        with self.assertRaisesRegex(WorkflowError, 'Заменить datadir'):
            self.workflow._rebuild(remote)
        self.workflow._restore_services.assert_called_once()
        self.assertFalse(any(c.startswith('rm -rf /tmp/dbrepair-mysql-rebuild-') for c,_ in remote.commands))
        self.assertTrue(any('tail -n 60' in c for c,_ in remote.commands))

    def test_recovery_can_run_after_cancellation(self):
        remote = Remote()
        event = threading.Event()
        event.set()
        remote.cancel_event = event
        original = remote.run
        def run(command, **kwargs):
            self.assertIsNone(remote.cancel_event)
            return original(command, **kwargs)
        remote.run = run
        self.assertTrue(self.workflow._restore_services(remote, '/tmp/dbrepair-mysql-rebuild-test'))
        self.assertIs(remote.cancel_event, event)

    def test_private_file_mode_precedes_password_write(self):
        remote = RemoteClient(self.config.connection, Mock())
        file = Mock()
        file.__enter__ = Mock(return_value=file)
        file.__exit__ = Mock(return_value=False)
        remote._sftp = Mock()
        remote._sftp.file.return_value = file
        remote.write_private_text('/tmp/test', 'secret')
        remote._sftp.file.assert_called_once_with('/tmp/test', mode='wx')
        self.assertEqual(file.method_calls, [('chmod', (0o600,), {}), ('write', (b'secret',), {})])

    def test_generated_scripts_parse(self):
        sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
        w = self.workflow
        commands = [w._preflight_command(), w._download_archives_command('/tmp/work'),
                    w._extract_archives_command('/tmp/work'), w._prepare_datadir_command('/tmp/work'),
                    w._replace_datadir_command('/tmp/work'), w._rollback_command('/tmp/work'),
                    w._stop_mysql_command(), w._import_schema_command('/tmp/work', '/tmp/client'),
                    w._import_version_command('/tmp/work', '/tmp/client'), w._schema_check_command('/tmp/client'),
                    w._restore_ukm_database_account_command('/tmp/client'), w._verify_ukm_database_account_command(),
                    w._start_ukmclient_command(), _mysql_ready_command(1)]
        for command in commands:
            with self.subTest(command=command[:60]):
                result = subprocess.run([sh, '-n'], input=command, text=True, encoding='utf-8', capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_copy_failure_and_owner_repair_and_rollback(self):
        sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
        for fail_copy in (True, False):
            with self.subTest(fail_copy=fail_copy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                mysql = root / 'mysql'
                (mysql / 'var').mkdir(parents=True)
                (mysql / 'var' / 'original').write_text('old')
                new = root / 'archive'
                new.mkdir()
                (new / 'replacement').write_text('new')
                work = root / 'work'
                work.mkdir()
                (work / 'new_var').write_text(new.as_posix())
                w = MysqlRebuildWorkflow(replace(self.config, services=ServiceCommands(mysql_stop='true', mysql_start='true', ukmclient_stop='true', ukmclient_start='true')), Mock())
                script = w._replace_datadir_command(work.as_posix()).replace('/usr/local/mysql', mysql.as_posix())
                shim = 'pidof() { return 1; }\nchown() { echo owner-normalized; }\n'
                if fail_copy:
                    shim += 'cp() { return 44; }\n'
                result = subprocess.run([sh, '-s'], input=shim+script, text=True, encoding='utf-8', capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 44 if fail_copy else 0, result.stderr)
                self.assertTrue((work / 'previous_datadir').exists())
                self.assertTrue((mysql / 'var_bad0' / 'original').exists())
                if not fail_copy:
                    self.assertIn('owner-normalized', result.stdout)
                script = w._rollback_command(work.as_posix()).replace('/usr/local/mysql', mysql.as_posix())
                result = subprocess.run([sh, '-s'], input='pidof() { return 1; }\n'+script, text=True, encoding='utf-8', capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((mysql / 'var' / 'original').exists())
                self.assertFalse((work / 'previous_datadir').exists())
                if not fail_copy:
                    self.assertEqual(len(list(mysql.glob('var_bad0.failed-*'))), 1)

    def test_running_mysql_prevents_datadir_change(self):
        sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
        script = self.workflow._stop_mysql_command().replace(self.config.services.mysql_stop, 'true')
        result = subprocess.run([sh, '-s'], input='pidof() { return 0; }\nsleep() { :; }\n'+script,
                                text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('замена datadir запрещена', result.stderr)

    def test_transient_and_persistent_io_wait(self):
        sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
        for persistent in (False, True):
            shim = 'pidof() { echo 123; }\nsleep() { :; }\nsamples=0\n'
            shim += 'grep() { samples=$((samples + 1)); '
            shim += 'return 0; }\n' if persistent else 'test "$samples" -eq 1; }\n'
            result = subprocess.run([sh, '-s'], input=shim+_mysql_io_guard(), text=True, encoding='utf-8',
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1 if persistent else 0, result.stderr)

    def test_account_sql_and_client_options_preserve_special_password(self):
        sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
        with tempfile.TemporaryDirectory() as directory:
            rc = Path(directory) / 'rc.ukm'
            rc.write_text("base=ukmclient\nuser=ukm_terminal\npassword=pa'ss\\word\"#\n")
            command = self.workflow._restore_ukm_database_account_command('/tmp/test.cnf')
            command = command.replace('/usr/local/ukmclient/rc.ukm', rc.as_posix())
            result = subprocess.run([sh, '-s'], input='mysql() { cat; }\n'+command, text=True,
                                    encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("IDENTIFIED BY 'pa\\'ss\\\\word\"#'", result.stdout)
            command = self.workflow._verify_ukm_database_account_command(check_process=False)
            command = command.replace('/usr/local/ukmclient/rc.ukm', rc.as_posix())
            # Inspect the options before the script's trap removes them.
            shim = 'mysql() { case "$*" in *"SHOW TABLES"*) echo trm_in_store;; *) cat "${1#--defaults-file=}";; esac; }\n'
            result = subprocess.run([sh, '-s'], input=shim+command.replace('>/dev/null\ni=0', '\ni=0').replace("LIMIT 1\" >/dev/null", "LIMIT 1\""),
                                    text=True, encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('password="pa\'ss\\\\word\\"#"', result.stdout)


if __name__ == '__main__':
    unittest.main()
