from __future__ import annotations

import shlex
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Callable, Sequence
import re

from .config import AppConfig
from .remote import RemoteClient
from .workflow import WorkflowError, WorkflowStep


MYSQL_REBUILD_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep("preflight", "1", "Проверить кассу и дистрибутивы на сервере UKM"),
    WorkflowStep("rebuild", "2", "Пересобрать чистый datadir и загрузить базу UKM"),
    WorkflowStep("verify", "3", "Проверить MySQL, схему UKM и запуск ukmclient"),
)

ProgressCallback = Callable[[WorkflowStep, str, str | None], None]


class MysqlRebuildWorkflow:
    """Controlled replacement for the legacy create_mysql.sh script."""

    def __init__(
        self,
        config: AppConfig,
        logger,
        *,
        cancel_event: threading.Event | None = None,
        source_server: str | None = None,
    ):
        self.config = config
        self.logger = logger
        self.cancel_event = cancel_event
        self.source_server = _validate_source_server(source_server)
        if not re.fullmatch(r"[A-Za-z0-9_]+", config.database.name):
            raise ValueError("Имя базы MySQL должно содержать только латинские буквы, цифры и подчёркивания.")

    def run_steps(self, step_ids: Sequence[str], *, progress: ProgressCallback | None = None) -> None:
        known_ids = {step.step_id for step in MYSQL_REBUILD_STEPS}
        unknown = set(step_ids) - known_ids
        if not step_ids or unknown:
            raise WorkflowError(f"Unknown MySQL rebuild steps: {', '.join(sorted(unknown)) or 'empty plan'}.")

        with RemoteClient(self.config.connection, self.logger, cancel_event=self.cancel_event) as remote:
            selected = [step for step in MYSQL_REBUILD_STEPS if step.step_id in step_ids]
            for step in selected:
                self.logger.info("Step %s: %s", step.number, step.title)
                if progress:
                    progress(step, "running", None)
                try:
                    if step.step_id == "rebuild":
                        self._rebuild(remote)
                    elif step.step_id == "verify":
                        self._verify_with_database_password(remote)
                    else:
                        remote.run(self._preflight_command(), timeout=90, use_sudo=True)
                except Exception as exc:
                    if progress:
                        progress(step, "error", str(exc))
                    raise
                if progress:
                    progress(step, "success", None)

    def _rebuild(self, remote: RemoteClient) -> None:
        """Пересобрать MySQL наблюдаемыми фазами с отдельными тайм-аутами."""
        workdir = f"/tmp/dbrepair-mysql-rebuild-{uuid.uuid4().hex}"
        recovery_required = False
        completed = False
        try:
            self._run_rebuild_phase(remote, "Проверить кассу перед пересборкой", self._preflight_command(), 90)
            self._run_rebuild_phase(remote, "Скачать архивы UKM", self._download_archives_command(workdir), 300)
            self._run_rebuild_phase(remote, "Распаковать архивы UKM", self._extract_archives_command(workdir), 900)
            self._run_rebuild_phase(remote, "Подготовить чистый datadir", self._prepare_datadir_command(workdir), 90)
            recovery_required = True
            self._run_rebuild_phase(remote, "Остановить ukmclient", self.config.services.ukmclient_stop, 90, get_pty=True)
            self._run_rebuild_phase(remote, "Остановить MySQL", self._stop_mysql_command(), 90, get_pty=True)
            self._run_rebuild_phase(remote, "Заменить datadir и сохранить предыдущий", self._replace_datadir_command(workdir), 180)
            self._run_rebuild_phase(remote, "Запустить MySQL", self.config.services.mysql_start, 300, get_pty=True)
            self._run_rebuild_phase(remote, "Дождаться готовности MySQL", _mysql_ready_command(300), 310)
            with self._mysql_credentials(remote) as client:
                self._run_rebuild_phase(remote, "Загрузить схему UKM", self._import_schema_command(workdir, client), 600)
                self._run_rebuild_phase(remote, "Загрузить версию UKM", self._import_version_command(workdir, client), 180)
                self._run_rebuild_phase(remote, "Проверить схему UKM", self._schema_check_command(client), 90)
                self._run_rebuild_phase(remote, "Восстановить доступ UKM к MySQL", self._restore_ukm_database_account_command(client), 90)
            self._run_rebuild_phase(remote, "Проверить вход UKM", self._verify_ukm_database_account_command(check_process=False), 30)
            self._run_rebuild_phase(remote, "Запустить ukmclient", self._start_ukmclient_command(), 90, get_pty=True)
            self._run_rebuild_phase(
                remote,
                "Проверить подключение UKM к MySQL",
                self._verify_ukm_database_account_command(),
                90,
            )
            completed = True
        except Exception:
            self._diagnose_mysql(remote)
            if recovery_required:
                self.logger.warning("Пересборка прервана; выполняется возврат предыдущего datadir и служб.")
                completed = self._restore_services(remote, workdir)
            raise
        finally:
            if completed or not recovery_required:
                try:
                    remote.run(f"rm -rf {shlex.quote(workdir)}", check=False, timeout=60, use_sudo=True)
                except Exception:
                    self.logger.warning("Не удалось удалить временный каталог пересборки MySQL: %s", workdir)
            else:
                self.logger.warning("Данные для диагностики и отката сохранены в %s", workdir)

    def _run_rebuild_phase(self, remote: RemoteClient, title: str, command: str, timeout: int, *, get_pty: bool = False) -> None:
        self.logger.info("Фаза пересборки MySQL: %s", title)
        try:
            remote.run(command, timeout=timeout, get_pty=get_pty or self.config.connection.use_sudo, use_sudo=True)
        except TimeoutError as exc:
            raise WorkflowError(f"Истекло время ожидания фазы «{title}» ({timeout} с).") from exc

    def _diagnose_mysql(self, remote: RemoteClient) -> None:
        try:
            remote.run("ps | grep '[m]ysqld' || true\nfor log in /usr/local/mysql/var/*.err; do\n"
                       "test -f \"$log\" || continue\necho \"Журнал MySQL: $log\"\ntail -n 60 \"$log\"\ndone",
                       check=False, use_sudo=True, timeout=20)
        except Exception as exc:
            self.logger.warning("Не удалось получить диагностику MySQL: %s", exc)

    def _restore_services(self, remote: RemoteClient, workdir: str) -> bool:
        # Cancellation must not disable the recovery SSH commands themselves.
        cancel_event = remote.cancel_event
        remote.cancel_event = None
        try:
            remote.run(self._rollback_command(workdir), timeout=180, use_sudo=True, get_pty=True)
            remote.run(self.config.services.mysql_start, timeout=300, use_sudo=True, get_pty=True)
            remote.run(_mysql_ready_command(60), timeout=70, use_sudo=True)
            remote.run(self._start_ukmclient_command(), timeout=90, use_sudo=True, get_pty=True)
            return True
        except Exception as exc:
            self.logger.warning("Не удалось завершить безопасный откат: %s", exc)
            return False
        finally:
            remote.cancel_event = cancel_event

    def _stop_mysql_command(self) -> str:
        return "set -eu\n" + _mysql_io_guard() + self.config.services.mysql_stop + "\n" + _mysql_stopped_guard()

    def _rollback_command(self, workdir: str) -> str:
        return f'''set -eu
WORK={shlex.quote(workdir)}
if test -f "$WORK/previous_datadir"; then
    {_mysql_io_guard()}
    {self.config.services.ukmclient_stop}
    {self.config.services.mysql_stop}
    {_mysql_stopped_guard()}
    BACKUP=$(cat "$WORK/previous_datadir")
    case "$BACKUP" in /usr/local/mysql/var_bad[0-9]*) ;; *) exit 1;; esac
    if test -d "$BACKUP"; then
        if test -e /usr/local/mysql/var; then
            mv /usr/local/mysql/var "$BACKUP.failed-{uuid.uuid4().hex}"
        fi
        mv "$BACKUP" /usr/local/mysql/var
        echo 'Предыдущий datadir восстановлен; неудачный datadir сохранён рядом'
    fi
    rm -f "$WORK/previous_datadir"
fi
'''

    def _download_archives_command(self, workdir: str) -> str:
        return f'''set -eu
RC=/usr/local/ukmclient/rc.ukm
WORK={shlex.quote(workdir)}
''' + self._source_server_resolver() + r'''
rm -rf "$WORK"
mkdir -p "$WORK"
resolve_source_server
cd "$WORK"
echo "Загрузка дистрибутивов с http://$server/ukminstall"
wget -q --timeout=30 --tries=2 -O ukmcli-build.tgz "http://$server/ukminstall/ukmcli-build.tgz"
wget -q --timeout=30 --tries=2 -O ukm-root.tar.gz "http://$server/ukminstall/ukm-root.tar.gz"
test -s ukmcli-build.tgz
test -s ukm-root.tar.gz
echo 'Архивы UKM скачаны'
'''

    @staticmethod
    def _extract_archives_command(workdir: str) -> str:
        return fr'''set -eu
WORK={shlex.quote(workdir)}
cd "$WORK"
tar xzf ukmcli-build.tgz
tar xzf ukm-root.tar.gz
echo 'Архивы UKM распакованы'
'''

    @staticmethod
    def _prepare_datadir_command(workdir: str) -> str:
        return f'''set -eu
WORK={shlex.quote(workdir)}
NEW_VAR=$(find "$WORK" -type d -path '*/usr/local/mysql*/var' -print | sed -n '1p')
SCHEMA_DUMP=$(find "$WORK" -type f -name 'ukm.sql' -print | sed -n '1p')
VERSION_DUMP=$(find "$WORK" -type f -name 'setver.sql' -print | sed -n '1p')
test -n "$NEW_VAR"
test -f "$NEW_VAR/ibdata1" -o -d "$NEW_VAR/mysql"
test -s "$SCHEMA_DUMP"
test -s "$VERSION_DUMP"
# Ensure there is room for the new datadir while keeping the previous one.
REQUIRED_KB=$(du -sk "$NEW_VAR" | awk '{{print $1}}')
AVAILABLE_KB=$(df -Pk /usr/local/mysql | awk 'NR==2 {{print $4}}')
test -n "$REQUIRED_KB" && test -n "$AVAILABLE_KB"
test "$AVAILABLE_KB" -gt "$((REQUIRED_KB + 65536))" || {{ echo 'Недостаточно свободного места для datadir и резерва 64 МБ' >&2; exit 1; }}
printf '%s\n' "$NEW_VAR" > "$WORK/new_var"
printf '%s\n' "$SCHEMA_DUMP" > "$WORK/schema_dump"
printf '%s\n' "$VERSION_DUMP" > "$WORK/version_dump"
echo 'Чистый datadir и дампы UKM подготовлены'
'''

    def _replace_datadir_command(self, workdir: str) -> str:
        return f'''set -eu
MYSQL_DIR=/usr/local/mysql
VAR_DIR=/usr/local/mysql/var
WORK={shlex.quote(workdir)}
NEW_VAR=$(cat "$WORK/new_var")
test -d "$NEW_VAR"
''' + _mysql_stopped_guard() + r'''
# Archive UIDs may belong to a different POS image. Preserve the local owner.
MYSQL_OWNER=$(stat -c '%u:%g' "$VAR_DIR")
case "$MYSQL_OWNER" in ''|*[!0-9:]*) echo 'Не удалось определить владельца datadir' >&2; exit 1;; esac
i=0
while test -e "$MYSQL_DIR/var_bad$i"; do i=$((i + 1)); done
BACKUP="$MYSQL_DIR/var_bad$i"
# Write the recovery marker before the rename, including copy failures.
printf '%s\n' "$BACKUP" > "$WORK/previous_datadir"
mv "$VAR_DIR" "$BACKUP"
cp -a "$NEW_VAR" "$VAR_DIR"
chown -R "$MYSQL_OWNER" "$VAR_DIR"
echo "Предыдущий datadir сохранён: $BACKUP"
'''

    def _import_schema_command(self, workdir: str, client: str) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
WORK={shlex.quote(workdir)}
DB_NAME={database_name}
SCHEMA_DUMP=$(cat "$WORK/schema_dump")
test -s "$SCHEMA_DUMP"
# The UKM distribution and POS MySQL configuration use UTF-8.  Do not use
# latin1 here: tables without an explicit charset inherit the database charset
# and incoming Cyrillic data would be converted to question marks.
mysql --defaults-file={shlex.quote(client)} -e "CREATE DATABASE IF NOT EXISTS \\`$DB_NAME\\` CHARACTER SET utf8 COLLATE utf8_general_ci"
mysql --defaults-file={shlex.quote(client)} "$DB_NAME" < "$SCHEMA_DUMP"
echo "Схема UKM загружена из $SCHEMA_DUMP"
'''

    def _import_version_command(self, workdir: str, client: str) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
WORK={shlex.quote(workdir)}
DB_NAME={database_name}
VERSION_DUMP=$(cat "$WORK/version_dump")
test -s "$VERSION_DUMP"
mysql --defaults-file={shlex.quote(client)} "$DB_NAME" < "$VERSION_DUMP"
echo "Версия UKM загружена из $VERSION_DUMP"
'''

    def _schema_check_command(self, client: str) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
DB_NAME={database_name}
mysql --defaults-file={shlex.quote(client)} "$DB_NAME" -N -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
TABLE_COUNT=$(mysql --defaults-file={shlex.quote(client)} "$DB_NAME" -N -e 'SHOW TABLES' | wc -l | tr -d '[:space:]')
test "$TABLE_COUNT" -ge 100 || {{ echo "UKM schema is incomplete: only $TABLE_COUNT tables" >&2; exit 1; }}
DB_CHARSET=$(mysql --defaults-file={shlex.quote(client)} -N -e "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='$DB_NAME'")
test "$DB_CHARSET" = utf8 || {{ echo "UKM database charset must be utf8, got: $DB_CHARSET" >&2; exit 1; }}
USER_NAME_CHARSET=$(mysql --defaults-file={shlex.quote(client)} -N -e "SELECT CHARACTER_SET_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB_NAME' AND TABLE_NAME='trm_in_users' AND COLUMN_NAME='name'")
test "$USER_NAME_CHARSET" = utf8 || {{ echo "UKM cashier names must use utf8, got: $USER_NAME_CHARSET" >&2; exit 1; }}
echo "Схема UKM проверена: $TABLE_COUNT таблиц"
'''

    def _start_ukmclient_command(self) -> str:
        return "set -eu\nTERM=linux " + self.config.services.ukmclient_start + "\n" + """
i=0; while [ \"$i\" -lt 30 ]; do
    if pidof cashmain >/dev/null 2>&1 || pidof ukmclient >/dev/null 2>&1; then exit 0; fi
    sleep 1
    i=$((i + 1))
done
echo 'cashmain did not start' >&2
exit 1"""

    def _verify_with_database_password(self, remote: RemoteClient) -> None:
        with self._mysql_credentials(remote) as client:
            self._run_rebuild_phase(remote, "Проверить схему UKM", self._schema_check_command(client), 90)
            self._run_rebuild_phase(remote, "Восстановить доступ UKM к MySQL", self._restore_ukm_database_account_command(client), 90)
            self._run_rebuild_phase(remote, "Проверить вход UKM", self._verify_ukm_database_account_command(check_process=False), 30)
            self._run_rebuild_phase(remote, "Запустить ukmclient", self._start_ukmclient_command(), 90, get_pty=True)
            self._run_rebuild_phase(remote, "Проверить подключение UKM к MySQL", self._verify_ukm_database_account_command(), 90)

    @contextmanager
    def _mysql_credentials(self, remote: RemoteClient):
        """Use one proven login for import and verification; never reset root implicitly."""
        path = f"/tmp/.dbrepair-mysql-client-{uuid.uuid4().hex}.cnf"
        try:
            for password in dict.fromkeys((self.config.database.password, "")):
                escaped = password.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
                remote.write_private_text(path, f'[client]\nuser=root\npassword="{escaped}"\nprotocol=socket\n')
                result = remote.run(f"mysql --defaults-file={shlex.quote(path)} -N -e 'SELECT 1'",
                                    check=False, timeout=30, use_sudo=True)
                if result.exit_status == 0:
                    if not password:
                        self.logger.warning("MySQL root доступен без пароля (чистый datadir); используется проверенный вход через локальный сокет.")
                    yield path
                    return
                remote.run(f"rm -f {shlex.quote(path)}", timeout=30, use_sudo=True)
                if not re.search(r"(?:ERROR\s+)?1045\b", result.stdout + result.stderr):
                    raise WorkflowError("Не удалось подключиться к MySQL через локальный сокет. Проверьте запуск MySQL и его журнал.")
            raise WorkflowError("MySQL отклонил пароль root из [database].password и вход без пароля. Укажите действующий пароль MySQL; пароль SSH задаётся отдельно.")
        finally:
            try:
                remote.run(f"rm -f {shlex.quote(path)}", check=False, timeout=30, use_sudo=True)
            except Exception:
                self.logger.warning("Не удалось удалить закрытый файл доступа MySQL: %s", path)

    def _restore_ukm_database_account_command(self, client: str) -> str:
        """Restore the local UKM account from rc.ukm without exposing its password."""
        database_name = shlex.quote(self.config.database.name)
        return fr'''set -eu
RC=/usr/local/ukmclient/rc.ukm
DB_NAME={database_name}
test -r "$RC" || {{ echo 'UKM configuration rc.ukm is unavailable' >&2; exit 1; }}
read_rc_value() {{ grep "^$1=" "$RC" | head -n 1 | cut -d= -f2-; }}
CONFIG_DB=$(read_rc_value base)
UKM_USER=$(read_rc_value user)
UKM_PASSWORD=$(read_rc_value password)
test -n "$CONFIG_DB" && test -n "$UKM_USER" && test -n "$UKM_PASSWORD" || {{ echo 'rc.ukm does not contain database credentials' >&2; exit 1; }}
test "$CONFIG_DB" = "$DB_NAME" || {{ echo "rc.ukm database $CONFIG_DB does not match $DB_NAME" >&2; exit 1; }}
# rc.ukm is a key=value file.  Escape only for the SQL sent on stdin; neither
# the password nor the generated SQL is written to the application log.
sql_escape() {{ printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g"; }}
SQL_USER=$(sql_escape "$UKM_USER")
SQL_PASSWORD=$(sql_escape "$UKM_PASSWORD")
mysql --defaults-file={shlex.quote(client)} <<EOF
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$SQL_USER'@'localhost' IDENTIFIED BY '$SQL_PASSWORD' WITH GRANT OPTION;
GRANT SELECT, INSERT, UPDATE, DELETE ON mysql.* TO '$SQL_USER'@'localhost';
GRANT RELOAD ON *.* TO '$SQL_USER'@'localhost';
FLUSH PRIVILEGES;
EOF
echo 'UKM MySQL account restored'
'''

    def _verify_ukm_database_account_command(self, *, check_process: bool = True) -> str:
        """Prove that UKM can log in and that its actual cash process remains alive."""
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
RC=/usr/local/ukmclient/rc.ukm
DB_NAME={database_name}
read_rc_value() {{ grep "^$1=" "$RC" | head -n 1 | cut -d= -f2-; }}
CONFIG_DB=$(read_rc_value base)
UKM_USER=$(read_rc_value user)
UKM_PASSWORD=$(read_rc_value password)
test "$CONFIG_DB" = "$DB_NAME" || {{ echo "rc.ukm database $CONFIG_DB does not match $DB_NAME" >&2; exit 1; }}
umask 077
CLIENT_FILE=$(mktemp /tmp/.dbrepair-ukm-client.XXXXXX)
cnf_escape() {{ printf '%s' "$1" | sed -e 's/\\\\/\\\\\\\\/g' -e 's/"/\\\\"/g'; }}
printf '[client]\\nuser="%s"\\npassword="%s"\\nhost=127.0.0.1\\nprotocol=tcp\\n' "$(cnf_escape "$UKM_USER")" "$(cnf_escape "$UKM_PASSWORD")" > "$CLIENT_FILE"
cleanup() {{ rm -f "$CLIENT_FILE"; }}
trap cleanup EXIT HUP INT TERM
mysql --defaults-file="$CLIENT_FILE" -D "$DB_NAME" -N -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
mysql --defaults-file="$CLIENT_FILE" -N -e "SELECT User FROM mysql.db WHERE Db='$DB_NAME' LIMIT 1" >/dev/null
''' + ('' if check_process else "exit 0\n") + '''
i=0; while [ "$i" -lt 30 ]; do
    if pidof cashmain >/dev/null 2>&1 || pidof ukmclient >/dev/null 2>&1; then
        echo 'UKM MySQL access and cash process verified'
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done
echo 'cashmain did not remain running after MySQL verification' >&2
exit 1
'''

    def _preflight_command(self) -> str:
        return r"""set -u
fail() { echo "Preflight failed: $1" >&2; exit 1; }
RC=/usr/local/ukmclient/rc.ukm
""" + self._source_server_resolver() + r"""
echo "Check: source server"
test "$(id -u)" -eq 0 || fail 'нужен root или включённый [connection].use_sudo'
resolve_source_server || fail 'cannot determine source server'
echo "Check: required utilities"
command -v wget >/dev/null 2>&1 || fail 'wget is unavailable'
command -v tar >/dev/null 2>&1 || fail 'tar is unavailable'
command -v mysql >/dev/null 2>&1 || fail 'mysql is unavailable'
command -v mysqladmin >/dev/null 2>&1 || fail 'mysqladmin is unavailable'
command -v pidof >/dev/null 2>&1 || fail 'pidof is unavailable'
test -r "$RC" || fail 'rc.ukm недоступен'
for key in base user password; do
    grep "^$key=." "$RC" >/dev/null || fail "rc.ukm: отсутствует $key"
done
echo "Check: MySQL datadir"
test -d /usr/local/mysql || fail 'directory /usr/local/mysql is absent'
test -d /usr/local/mysql/var || fail 'directory /usr/local/mysql/var is absent'
echo "Check: blocked MySQL disk I/O"
""" + _mysql_io_guard() + f"""
test "$(grep '^base=' \"$RC\" | head -n 1 | cut -d= -f2-)" = {shlex.quote(self.config.database.name)} || fail 'Имя базы в rc.ukm не совпадает с конфигурацией'
""" + r"""
echo "Check: ukmcli-build.tgz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukmcli-build.tgz" || fail "cannot download ukmcli-build.tgz from $server"
echo "Check: ukm-root.tar.gz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukm-root.tar.gz" || fail "cannot download ukm-root.tar.gz from $server"
echo Source: http://$server/ukminstall"""

    def _source_server_resolver(self) -> str:
        """Return POS shell code that finds the UKM package source without executing rc.ukm."""
        configured_server = shlex.quote(self.source_server) if self.source_server else "''"
        return f"""manual_server={configured_server}
""" + r"""
resolve_source_server() {
    server=
    source_method=
    # Manual value is necessary when MySQL is stopped and netstat has no peer.
    if test -n "$manual_server"; then
        server=$manual_server
        source_method='manual input'
    fi
    # Old POS images can contain a plain server= line.  Do not source rc.ukm:
    # it may execute terminal-specific commands in a non-interactive shell.
    if test -z "${server:-}" && test -r "$RC"; then
        server=$(awk -F= '/^[[:space:]]*server[[:space:]]*=/ { print $2; exit }' "$RC" | tr -d '[:space:]"')
        if test -n "${server:-}"; then source_method="rc.ukm"; fi
    fi
    # Modern POS images do not store the update server in rc.ukm.  The UKM
    # server has an established connection to the local MySQL port, so use its
    # single non-loopback peer as the package source.
    if test -z "${server:-}"; then
        command -v netstat >/dev/null 2>&1 || { echo 'netstat is unavailable' >&2; return 1; }
        peers=$(netstat -tn 2>/dev/null | awk '$6 == "ESTABLISHED" && $4 ~ /(:3306|:mysql)$/ { peer=$5; sub(/:[^:]*$/, "", peer); if (peer !~ /^(127\.|localhost|::1)/) print peer }' | sort -u)
        peer_count=$(printf '%s\n' "$peers" | sed '/^$/d' | wc -l | tr -d '[:space:]')
        if test "$peer_count" -eq 0; then
            echo 'no established remote MySQL peer was found by netstat' >&2
            return 1
        fi
        if test "$peer_count" -ne 1; then
            echo "multiple remote MySQL peers found: $peers" >&2
            return 1
        fi
        server=$peers
        source_method='netstat MySQL peer'
    fi
    case "$server" in *[!0-9A-Za-z._:-]*) echo "unsafe source server value: $server" >&2; return 1;; esac
    echo "Source server: $server ($source_method)"
}
"""


def _mysql_stopped_guard() -> str:
    return '''i=0
while pidof mysqld mysqld_safe >/dev/null 2>&1; do
    test "$i" -lt 30 || { echo 'MySQL не остановлен; замена datadir запрещена' >&2; exit 1; }
    sleep 1
    i=$((i + 1))
done
'''


def _mysql_io_guard() -> str:
    return '''# A brief D state is normal I/O; confirm it persists before refusing.
io_sample=0
while test "$io_sample" -lt 3; do
    io_blocked=0
    for mysql_pid in $(pidof mysqld 2>/dev/null || true); do
        if grep -E '^State:[[:space:]]*D' "/proc/$mysql_pid/status" >/dev/null 2>&1; then
            io_blocked=1
        fi
    done
    test "$io_blocked" -eq 1 || break
    io_sample=$((io_sample + 1))
    sleep 1
done
if test "$io_sample" -eq 3; then
    echo 'mysqld продолжает ждать дисковый ввод-вывод (D). Изменение datadir запрещено; проверьте состояние диска и журнал MySQL.' >&2
    exit 1
fi
'''


def _mysql_ready_command(seconds: int) -> str:
    return f'''set -eu
i=0
while [ "$i" -lt {seconds} ]; do
    mysqladmin --no-defaults --protocol=socket ping --silent >/dev/null 2>&1 && exit 0
    sleep 1
    i=$((i + 1))
done
echo 'MySQL did not become ready in time' >&2
exit 1
'''


def _validate_source_server(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    if not re.fullmatch(r"[0-9A-Za-z._:-]+", candidate):
        raise ValueError("Адрес сервера UKM должен содержать только имя хоста, IP-адрес и необязательный порт.")
    return candidate
