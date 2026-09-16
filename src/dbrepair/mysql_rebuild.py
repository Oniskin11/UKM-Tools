from __future__ import annotations

import threading
import uuid
from typing import Callable, Sequence

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

    def __init__(self, config: AppConfig, logger, *, cancel_event: threading.Event | None = None):
        self.config = config
        self.logger = logger
        self.cancel_event = cancel_event

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
                    command = {
                        "preflight": self._preflight_command,
                        "rebuild": self._rebuild_command,
                        "verify": self._verify_command,
                    }[step.step_id]()
                    remote.run(command, timeout=900 if step.step_id == "rebuild" else 90)
                    if step.step_id == "rebuild":
                        self._apply_standard_grants(remote)
                except Exception as exc:
                    if progress:
                        progress(step, "error", str(exc))
                    raise
                if progress:
                    progress(step, "success", None)

    def _apply_standard_grants(self, remote: RemoteClient) -> None:
        """Apply the legacy UKM local accounts without logging their password."""
        password = _sql_literal(self.config.database.password)
        path = f"/tmp/.dbrepair-mysql-grants-{uuid.uuid4().hex}.sql"
        grants = f"""DELETE FROM mysql.db;
DELETE FROM mysql.user;
GRANT ALL ON *.* TO root@'localhost' IDENTIFIED BY {password} WITH GRANT OPTION;
GRANT ALL ON *.* TO ukm_terminal@'localhost' IDENTIFIED BY {password} WITH MAX_USER_CONNECTIONS 100 GRANT OPTION;
GRANT ALL ON *.* TO ukm_web@'localhost' IDENTIFIED BY {password} WITH MAX_USER_CONNECTIONS 50;
FLUSH PRIVILEGES;
"""
        remote.write_text(path, grants)
        try:
            remote.run(f"mysql < {path}", timeout=60)
        finally:
            remote.run(f"rm -f {path}", check=False)

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
echo "Check: ukmcli-build.tgz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukmcli-build.tgz" || fail "cannot download ukmcli-build.tgz from $server"
echo "Check: ukm-root.tar.gz"
wget -q --spider --timeout=15 --tries=1 "http://$server/ukminstall/ukm-root.tar.gz" || fail "cannot download ukm-root.tar.gz from $server"
echo Source: http://$server/ukminstall"""

    def _rebuild_command(self) -> str:
        return r"""set -eu
MYSQL_DIR=/usr/local/mysql
VAR_DIR=/usr/local/mysql/var
RC=/usr/local/ukmclient/rc.ukm
fail() { echo "MySQL rebuild failed: $1" >&2; return 1; }
""" + self._source_server_resolver() + r"""
WORK=$(mktemp -d /tmp/dbrepair-mysql-rebuild.XXXXXX)
ukm_stopped=0
mysql_stopped=0
cleanup() {
    rc=$?
    rm -rf "$WORK"
    if [ "$mysql_stopped" -eq 1 ]; then /etc/init.d/mysql start || true; fi
    if [ "$ukm_stopped" -eq 1 ]; then /etc/init.d/ukmclient start || true; fi
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
NEW_VAR=$(find "$WORK" -type d -path '*/usr/local/mysql/var' -print -quit)
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
mysql -e 'CREATE DATABASE IF NOT EXISTS ukmclient CHARACTER SET utf8 COLLATE utf8_general_ci'
find "$WORK" -type f -name '*.sql' -print | LC_ALL=C sort -r | while IFS= read -r sql; do mysql ukmclient < "$sql"; done
echo "Previous datadir kept at $BACKUP"
"""

    @staticmethod
    def _source_server_resolver() -> str:
        """Return POS shell code that finds the UKM package source without executing rc.ukm."""
        return r"""
resolve_source_server() {
    server=
    source_method=
    # Old POS images can contain a plain server= line.  Do not source rc.ukm:
    # it may execute terminal-specific commands in a non-interactive shell.
    if test -r "$RC"; then
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

    def _verify_command(self) -> str:
        return """set -eu
mysqladmin ping --silent >/dev/null
mysql -N -e "SHOW DATABASES" | grep -Fx ukmclient >/dev/null
/etc/init.d/ukmclient start
i=0; while [ "$i" -lt 30 ]; do pgrep -x ukmclient >/dev/null 2>&1 && exit 0; sleep 1; i=$((i + 1)); done
echo 'ukmclient did not start' >&2
exit 1"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
