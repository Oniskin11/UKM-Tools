from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from unittest.mock import Mock, patch

import pytest

from dbrepair.mysql_rebuild import MysqlRebuildWorkflow, RebuildPausedError, CHECKPOINT
from dbrepair.remote import CommandResult, OperationCancelledError
from dbrepair.workflow import WorkflowError
from test_mysql_rebuild import Remote
import test_mysql_rebuild as helpers


@pytest.fixture
def workflow():
    setup = helpers.MysqlRebuildTests()
    setup.setUp()
    return setup.workflow


def test_paused_start_does_not_rollback_or_remove_state(workflow):
    remote = Remote()
    workflow._wait_remote_job = Mock(side_effect=RebuildPausedError('slow disk'))
    workflow._restore_services = Mock()
    with pytest.raises(RebuildPausedError):
        workflow._rebuild(remote)
    workflow._restore_services.assert_not_called()
    assert not any(c.startswith('rm -rf /tmp/dbrepair-mysql-rebuild-') for c,_ in remote.commands)
    assert len(remote.scripts) == 1
    assert '/etc/init.d/mysql start' in remote.scripts[0][1]
    assert not any(c == '/etc/init.d/mysql stop' for c,_ in remote.commands)


def test_wait_timeout_preserves_live_startup(workflow):
    remote = Remote()
    remote.run = Mock(return_value=CommandResult('', 0, 'JOB_RUNNING\nMYSQL_PROCESS\nib_logfile0 300MB', ''))
    with patch('dbrepair.mysql_rebuild.time.monotonic', side_effect=[0, 1, 7201]), patch('dbrepair.mysql_rebuild.time.sleep'):
        with pytest.raises(RebuildPausedError, match='MySQL не остановлен'):
            workflow._wait_remote_job(remote, '/tmp/work', 'mysql-start', 'Запуск', 7200, mysql_start=True)
    assert remote.run.call_count == 1


def test_wait_reconnects_after_temporary_ssh_failure(workflow):
    remote = Remote()
    remote.run = Mock(side_effect=[TimeoutError('channel blocked'), CommandResult('', 0, 'MYSQL_READY\n', '')])
    remote.close = Mock()
    remote.connect = Mock()
    with patch('dbrepair.mysql_rebuild.time.sleep'):
        workflow._wait_remote_job(remote, '/tmp/work', 'mysql-start', 'Запуск', 7200, mysql_start=True)
    remote.connect.assert_called_once()
    assert remote.run.call_count == 2


def test_start_init_exit_does_not_abort_while_mysqld_is_alive(workflow):
    remote = Remote()
    remote.run = Mock(side_effect=[CommandResult('', 0, 'JOB_EXIT=1\nMYSQL_PROCESS\n', ''), CommandResult('', 0, 'MYSQL_READY\n', '')])
    with patch('dbrepair.mysql_rebuild.time.sleep'):
        workflow._wait_remote_job(remote, '/tmp/work', 'mysql-start', 'Запуск', 7200, mysql_start=True)
    assert remote.run.call_count == 2


def test_cancel_preserves_background_job(workflow):
    workflow.cancel_event = threading.Event()
    workflow.cancel_event.set()
    remote = Remote()
    with pytest.raises(OperationCancelledError):
        workflow._wait_remote_job(remote, '/tmp/work', 'mysql-start', 'Запуск', 7200, mysql_start=True)
    assert not remote.commands


def test_detached_import_keeps_credentials_until_workdir_cleanup(workflow):
    remote = Remote()
    with pytest.raises(OperationCancelledError):
        with workflow._mysql_credentials(remote, credential_dir='/tmp/work'):
            raise OperationCancelledError('closed GUI')
    assert remote.written[0][0].startswith('/tmp/work/.dbrepair-mysql-client-')
    assert not any(c.startswith('rm -f /tmp/work/.dbrepair-mysql-client-') for c,_ in remote.commands)


def test_remote_shell_mode_skips_login_profiles(workflow):
    from dbrepair.remote import RemoteClient
    default = RemoteClient(workflow.config.connection, Mock())
    batch = RemoteClient(workflow.config.connection, Mock(), login_shell=False)
    assert '/bin/sh -lc' in default._wrap_command('true', cwd=None, use_sudo=False)
    assert batch._wrap_command('true', cwd=None, use_sudo=False) == 'TERM=linux /bin/sh -c true'


def test_new_rebuild_refuses_existing_checkpoint(workflow):
    workflow._checkpoint_path = Mock(return_value='/tmp/dbrepair-mysql-rebuild-' + 'a'*32)
    remote = Remote()
    with pytest.raises(WorkflowError, match='повторная замена'):
        workflow._rebuild(remote)
    assert not remote.commands


def test_resume_uses_existing_identity_and_skips_replacement(workflow):
    work = '/tmp/dbrepair-mysql-rebuild-' + 'a'*32
    remote = Remote()
    remote.run = Mock(return_value=CommandResult('',0,'DBREPAIR_PHASE=datadir_ready\n',''))
    workflow._checkpoint_path = Mock(return_value=work)
    workflow._finish_rebuild = Mock()
    workflow._resume(remote)
    workflow._finish_rebuild.assert_called_once_with(remote, work, 'datadir_ready')
    validation = remote.run.call_args_list[0].args[0]
    assert "stat -c '%d:%i'" in validation
    assert 'datadir_identity' in validation
    assert not any('cp -a' in call.args[0] for call in remote.run.call_args_list)


def test_schema_in_progress_is_reattached_not_imported_twice(workflow):
    remote = Remote()
    workflow._wait_remote_job = Mock()
    workflow._finish_rebuild(remote, '/tmp/work', 'schema_importing')
    assert all('/schema.' not in path for path,_ in remote.scripts)
    assert len(remote.scripts) == 1
    assert '/version.' in remote.scripts[0][0]
    assert workflow._wait_remote_job.call_args_list[0].args[2] == 'schema'


def test_failed_import_is_not_replayed(workflow):
    remote = Remote()
    remote.run = Mock(return_value=CommandResult('', 0, 'JOB_EXIT=1\n', ''))
    with pytest.raises(WorkflowError, match='кодом 1'):
        workflow._wait_remote_job(remote, '/tmp/work', 'schema', 'Импорт', 1800)
    assert not remote.scripts


def test_background_job_runs_once_and_records_failure_exit(workflow):
    sh = shutil.which('sh') or r'C:\Program Files\Git\bin\sh.exe'
    class ShellRemote:
        def write_text(self, path, text, **kwargs):
            Path(path).write_text(text, encoding='utf-8')
        def run(self, command, **kwargs):
            if 'nohup' in command:
                assert kwargs['get_pty'] is False
            result = subprocess.run([sh, '-s'], input=command, capture_output=True, text=True, encoding='utf-8', timeout=10)
            assert result.returncode == 0, result.stderr
            return CommandResult(command,result.returncode,result.stdout,result.stderr)
    remote = ShellRemote()
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory).as_posix()
        command = f'set -eu\nprintf x >> "{work}/once"\nsleep 0.1\nexit 7'
        workflow._launch_remote_job(remote, work, 'schema', command)
        for _ in range(30):
            if Path(directory, 'schema.exit').exists():
                break
            time.sleep(.1)
        workflow._launch_remote_job(remote, work, 'schema', command)
        assert Path(directory, 'schema.exit').read_text().strip() == '7'
        assert Path(directory, 'once').read_text() == 'x'
