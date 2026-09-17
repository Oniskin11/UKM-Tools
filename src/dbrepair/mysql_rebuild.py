from __future__ import annotations

import shlex
import threading
import uuid
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
                        remote.run(self._preflight_command(), timeout=90)
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
        try:
            self._run_rebuild_phase(remote, "Скачать архивы UKM", self._download_archives_command(workdir), 300)
            self._run_rebuild_phase(remote, "Распаковать архивы UKM", self._extract_archives_command(workdir), 900)
            self._run_rebuild_phase(remote, "Подготовить чистый datadir", self._prepare_datadir_command(workdir), 90)
            recovery_required = True
            self._run_rebuild_phase(remote, "Остановить ukmclient", "/etc/init.d/ukmclient stop", 90, get_pty=True)
            self._run_rebuild_phase(remote, "Остановить MySQL", "/etc/init.d/mysql stop", 90, get_pty=True)
            self._run_rebuild_phase(remote, "Заменить datadir и сохранить предыдущий", self._replace_datadir_command(workdir), 180)
            self._run_rebuild_phase(remote, "Запустить MySQL", "/etc/init.d/mysql start", 300, get_pty=True)
            self._run_rebuild_phase(remote, "Дождаться готовности MySQL", _mysql_ready_command(300), 310)
            self._run_rebuild_phase(remote, "Загрузить схему UKM", self._import_schema_command(workdir), 600)
            self._run_rebuild_phase(remote, "Загрузить версию UKM", self._import_version_command(workdir), 180)
            self._run_rebuild_phase(remote, "Проверить схему UKM", self._schema_check_command(), 90)
            self._run_rebuild_phase(
                remote,
                "Восстановить доступ UKM к MySQL",
                self._restore_ukm_database_account_command(),
                90,
            )
            self._run_rebuild_phase(remote, "Запустить ukmclient", self._start_ukmclient_command(), 90, get_pty=True)
            self._run_rebuild_phase(
                remote,
                "Проверить подключение UKM к MySQL",
                self._verify_ukm_database_account_command(),
                90,
            )
        except Exception:
            if recovery_required:
                self.logger.warning("Пересборка прервана; выполняется возврат служб MySQL и ukmclient.")
                self._restore_services(remote)
            raise
        finally:
            try:
                remote.run(f"rm -rf {shlex.quote(workdir)}", check=False, timeout=60)
            except Exception:
                self.logger.warning("Не удалось удалить временный каталог пересборки MySQL: %s", workdir)

    def _run_rebuild_phase(self, remote: RemoteClient, title: str, command: str, timeout: int, *, get_pty: bool = False) -> None:
        self.logger.info("Фаза пересборки MySQL: %s", title)
        try:
            remote.run(command, timeout=timeout, get_pty=get_pty)
        except TimeoutError as exc:
            raise WorkflowError(f"Истекло время ожидания фазы «{title}» ({timeout} с).") from exc

    def _restore_services(self, remote: RemoteClient) -> None:
        for command, service in (
            ("/etc/init.d/mysql start || true", "MySQL"),
            ("TERM=linux /etc/init.d/ukmclient start || true", "ukmclient"),
        ):
            try:
                remote.run(command, check=False, timeout=90, get_pty=True)
            except Exception as exc:
                self.logger.warning("Не удалось вернуть службу %s после ошибки пересборки: %s", service, exc)

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
i=0
while test -e "$MYSQL_DIR/var_bad$i"; do i=$((i + 1)); done
BACKUP="$MYSQL_DIR/var_bad$i"
mv "$VAR_DIR" "$BACKUP"
cp -a "$NEW_VAR" "$VAR_DIR"
printf '%s\\n' "$BACKUP" > "$WORK/previous_datadir"
echo "Предыдущий datadir сохранён: $BACKUP"
'''

    def _import_schema_command(self, workdir: str) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
WORK={shlex.quote(workdir)}
DB_NAME={database_name}
SCHEMA_DUMP=$(cat "$WORK/schema_dump")
test -s "$SCHEMA_DUMP"
        # UKM's legacy schema has composite VARCHAR(255) keys that exceed the
        # 1000-byte InnoDB limit of MySQL 5.0 under UTF-8.  The vendor dump is
        # therefore imported into its compatible one-byte character set.
        mysql -e "CREATE DATABASE IF NOT EXISTS \\`$DB_NAME\\` CHARACTER SET latin1 COLLATE latin1_swedish_ci"
mysql "$DB_NAME" < "$SCHEMA_DUMP"
echo "Схема UKM загружена из $SCHEMA_DUMP"
'''

    def _import_version_command(self, workdir: str) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
WORK={shlex.quote(workdir)}
DB_NAME={database_name}
VERSION_DUMP=$(cat "$WORK/version_dump")
test -s "$VERSION_DUMP"
mysql "$DB_NAME" < "$VERSION_DUMP"
echo "Версия UKM загружена из $VERSION_DUMP"
'''

    def _schema_check_command(self) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f'''set -eu
DB_NAME={database_name}
mysql "$DB_NAME" -N -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
TABLE_COUNT=$(mysql "$DB_NAME" -N -e 'SHOW TABLES' | wc -l | tr -d '[:space:]')
test "$TABLE_COUNT" -ge 100 || {{ echo "UKM schema is incomplete: only $TABLE_COUNT tables" >&2; exit 1; }}
echo "Схема UKM проверена: $TABLE_COUNT таблиц"
'''

    @staticmethod
    def _start_ukmclient_command() -> str:
        return """set -eu
TERM=linux /etc/init.d/ukmclient start
i=0; while [ \"$i\" -lt 30 ]; do
    if pidof cashmain >/dev/null 2>&1 || pidof ukmclient >/dev/null 2>&1; then exit 0; fi
    sleep 1
    i=$((i + 1))
done
echo 'cashmain did not start' >&2
exit 1"""

    def _verify_with_database_password(self, remote: RemoteClient) -> None:
        """Verify MySQL through a temporary client file so the password never reaches logs."""
        path = f"/tmp/.dbrepair-mysql-client-{uuid.uuid4().hex}.cnf"
        password = self.config.database.password.replace("\\", "\\\\").replace('"', '\\"')
        client_config = f'[client]\nuser=root\npassword="{password}"\n'
        remote.write_text(path, client_config)
        try:
            remote.run(f"chmod 600 {shlex.quote(path)}", timeout=30)
            # The POS login profile uses terminal commands before the command
            # body runs. A PTY supplies TERM during that profile startup.
            remote.run(self._verify_command(path), timeout=90, get_pty=True)
        finally:
            remote.run(f"rm -f {shlex.quote(path)}", check=False)

    def _restore_ukm_database_account_command(self) -> str:
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
sql_escape() {{ printf '%s' "$1" | sed -e 's/\\\\/\\\\\\\\/g' -e "s/'/\\\\'/g"; }}
SQL_USER=$(sql_escape "$UKM_USER")
SQL_PASSWORD=$(sql_escape "$UKM_PASSWORD")
mysql <<EOF
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$SQL_USER'@'localhost' IDENTIFIED BY '$SQL_PASSWORD' WITH GRANT OPTION;
GRANT SELECT, INSERT, UPDATE, DELETE ON mysql.* TO '$SQL_USER'@'localhost';
GRANT RELOAD ON *.* TO '$SQL_USER'@'localhost';
FLUSH PRIVILEGES;
EOF
echo 'UKM MySQL account restored'
'''

    def _verify_ukm_database_account_command(self) -> str:
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
CLIENT_FILE=$(mktemp /tmp/.dbrepair-ukm-client.XXXXXX)
umask 077
printf '[client]\\nuser=%s\\npassword=%s\\nhost=127.0.0.1\\n' "$UKM_USER" "$UKM_PASSWORD" > "$CLIENT_FILE"
cleanup() {{ rm -f "$CLIENT_FILE"; }}
trap cleanup EXIT HUP INT TERM
mysql --defaults-extra-file="$CLIENT_FILE" -D "$DB_NAME" -N -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
mysql --defaults-extra-file="$CLIENT_FILE" -N -e "SELECT User FROM mysql.db WHERE Db='$DB_NAME' LIMIT 1" >/dev/null
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
resolve_source_server || fail 'cannot determine source server'
echo "Check: required utilities"
command -v wget >/dev/null 2>&1 || fail 'wget is unavailable'
command -v tar >/dev/null 2>&1 || fail 'tar is unavailable'
command -v mysql >/dev/null 2>&1 || fail 'mysql is unavailable'
echo "Check: MySQL datadir"
test -d /usr/local/mysql || fail 'directory /usr/local/mysql is absent'
test -d /usr/local/mysql/var || fail 'directory /usr/local/mysql/var is absent'
echo "Check: blocked MySQL disk I/O"
# A process in D state cannot be stopped remotely.  Replacing its datadir
# would leave the POS with two competing MySQL states, so fail before any
# service or data change.  Keep the probe portable for old BusyBox images.
if ps -o stat= -C mysqld 2>/dev/null | grep -E '^[[:space:]]*D' >/dev/null; then
    fail 'mysqld is waiting for disk I/O (D state); repair the filesystem before rebuilding'
fi
echo "Check: ukmcli-build.tgz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukmcli-build.tgz" || fail "cannot download ukmcli-build.tgz from $server"
echo "Check: UKM schema dump"
wget -q --timeout=30 --tries=1 -O - "http://$server/ukminstall/ukmcli-build.tgz" | tar tzf - | grep -E '(^|/)ukm\.sql$' >/dev/null || fail 'ukmcli-build.tgz does not contain ukm.sql'
echo "Check: ukm-root.tar.gz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukm-root.tar.gz" || fail "cannot download ukm-root.tar.gz from $server"
echo Source: http://$server/ukminstall"""

    def _rebuild_command(self) -> str:
        database_name = shlex.quote(self.config.database.name)
        return f"""set -eu
MYSQL_DIR=/usr/local/mysql
VAR_DIR=/usr/local/mysql/var
RC=/usr/local/ukmclient/rc.ukm
DB_NAME={database_name}
fail() {{ echo "MySQL rebuild failed: $1" >&2; return 1; }}
""" + self._source_server_resolver() + r"""
WORK=$(mktemp -d /tmp/dbrepair-mysql-rebuild.XXXXXX)
ukm_stopped=0
mysql_stopped=0
cleanup() {
    rc=$?
    rm -rf "$WORK"
    if [ "$mysql_stopped" -eq 1 ]; then /etc/init.d/mysql start || true; fi
    if [ "$ukm_stopped" -eq 1 ]; then TERM=linux /etc/init.d/ukmclient start || true; fi
    exit "$rc"
}
trap cleanup EXIT HUP INT TERM
resolve_source_server || { fail 'cannot determine source server'; exit 1; }
cd "$WORK"
wget -q --timeout=30 --tries=2 -O ukmcli-build.tgz "http://$server/ukminstall/ukmcli-build.tgz"
wget -q --timeout=30 --tries=2 -O ukm-root.tar.gz "http://$server/ukminstall/ukm-root.tar.gz"
test -s ukmcli-build.tgz
test -s ukm-root.tar.gz
tar tzf ukmcli-build.tgz >/dev/null
tar tzf ukm-root.tar.gz >/dev/null
if tar tzf ukmcli-build.tgz | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then echo 'Unsafe build archive' >&2; exit 2; fi
if tar tzf ukm-root.tar.gz | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then echo 'Unsafe root archive' >&2; exit 2; fi
tar xzf ukmcli-build.tgz
tar xzf ukm-root.tar.gz
# The package ships a versioned directory such as mysql-5.0.67-ukm.
# BusyBox find on POS does not support GNU find's -quit predicate.
NEW_VAR=$(find "$WORK" -type d -path '*/usr/local/mysql*/var' -print | sed -n '1p')
test -n "$NEW_VAR"
test -f "$NEW_VAR/ibdata1" -o -d "$NEW_VAR/mysql"
/etc/init.d/ukmclient stop
ukm_stopped=1
/etc/init.d/mysql stop
mysql_stopped=1
i=0
while test -e "$MYSQL_DIR/var_bad$i"; do i=$((i + 1)); done
BACKUP="$MYSQL_DIR/var_bad$i"
mv "$VAR_DIR" "$BACKUP"
cp -a "$NEW_VAR" "$VAR_DIR"
/etc/init.d/mysql start
mysql_stopped=0
i=0; while [ "$i" -lt 60 ]; do mysqladmin ping --silent >/dev/null 2>&1 && break; sleep 1; i=$((i + 1)); done
mysqladmin ping --silent >/dev/null 2>&1
mysql -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET latin1 COLLATE latin1_swedish_ci"
# The build archive contains the canonical schema in ukm.sql.  Import it
# explicitly and prove that the primary terminal table exists.
SCHEMA_DUMP=$(find "$WORK" -type f -name 'ukm.sql' -print | sed -n '1p')
VERSION_DUMP=$(find "$WORK" -type f -name 'setver.sql' -print | sed -n '1p')
test -s "$SCHEMA_DUMP" || { echo 'UKM schema dump ukm.sql was not found' >&2; exit 1; }
test -s "$VERSION_DUMP" || { echo 'UKM version dump setver.sql was not found' >&2; exit 1; }
mysql "$DB_NAME" < "$SCHEMA_DUMP"
mysql "$DB_NAME" < "$VERSION_DUMP"
mysql "$DB_NAME" -N -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
TABLE_COUNT=$(mysql "$DB_NAME" -N -e 'SHOW TABLES' | wc -l | tr -d '[:space:]')
test "$TABLE_COUNT" -ge 100 || { echo "UKM schema is incomplete: only $TABLE_COUNT tables" >&2; exit 1; }
echo "UKM schema restored: $TABLE_COUNT tables"
echo "Previous datadir kept at $BACKUP"
"""

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

    def _verify_command(self, client_config: str) -> str:
        client_option = shlex.quote(f"--defaults-extra-file={client_config}")
        database_name = shlex.quote(self.config.database.name)
        return f"""set -eu
mysqladmin {client_option} ping --silent >/dev/null
mysql {client_option} -N -e "SHOW DATABASES" | grep -Fx {database_name} >/dev/null
mysql {client_option} -N {database_name} -e "SHOW TABLES LIKE 'trm_in_store'" | grep -Fx trm_in_store >/dev/null
TERM=linux /etc/init.d/ukmclient start
i=0; while [ "$i" -lt 30 ]; do
    if pidof cashmain >/dev/null 2>&1 || pidof ukmclient >/dev/null 2>&1; then exit 0; fi
    sleep 1
    i=$((i + 1))
done
echo 'cashmain did not start' >&2
exit 1"""


def _mysql_ready_command(seconds: int) -> str:
    return f'''set -eu
i=0
while [ "$i" -lt {seconds} ]; do
    mysqladmin ping --silent >/dev/null 2>&1 && exit 0
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
