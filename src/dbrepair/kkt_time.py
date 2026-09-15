from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .config import AppConfig
from .remote import RemoteClient

PPP_DIRECTORY = "/usr/local/ppp-pos2kkt"
PPP_STOP_SCRIPT = "ppp-pos2kkt-stop.sh"
PPP_START_SCRIPT = "ppp-pos2kkt-start.sh"
PPP_CASH_IP = "192.168.250.1"
PPP_KKT_IP = "192.168.250.2"
PPP_SERIAL_PORT = "/dev/ttyS0"
PPP_LOG_FILE = "/var/log/ppp-pos_ttyS0.log"
KKT_SSH_KEY = "/tmp/sp_kkt_rsa"
KKT_SSH_PORT = 60054
KKT_SSH_USER = "pi"


@dataclass(frozen=True)
class KktTimeStep:
    step_id: str
    number: str
    title: str


KKT_TIME_STEPS: tuple[KktTimeStep, ...] = (
    KktTimeStep("sync_time", "1", "Синхронизировать время ККТ"),
)

ProgressCallback = Callable[[KktTimeStep, str, str | None], None]


def build_sync_command() -> str:
    """Команда для кассы: PPP-параметры штатно фиксированы для её конфигурации."""
    stop_ppp = (
        f"sh -x ./{PPP_STOP_SCRIPT} 0 {PPP_CASH_IP} {PPP_KKT_IP} {PPP_SERIAL_PORT}"
    )
    start_ppp = (
        f"sh -x ./{PPP_START_SCRIPT} 0 {PPP_CASH_IP} {PPP_KKT_IP} {PPP_SERIAL_PORT}"
    )
    return f'''set -eu
ukmclient_stopped=0
ppp_started=0
cleanup() {{
    if [ "$ppp_started" -eq 1 ]; then
        cd {PPP_DIRECTORY} && {stop_ppp} || true
    fi
    if [ "$ukmclient_stopped" -eq 1 ]; then
        /etc/init.d/ukmclient start || true
    fi
}}
trap cleanup EXIT HUP INT TERM
/etc/init.d/ukmclient stop
ukmclient_stopped=1
cd {PPP_DIRECTORY}
{stop_ppp}
if {start_ppp}; then
    ppp_started=1
else
    ppp_start_status=$?
    # Код 2 штатно возвращается, когда PPP уже поднят без pid-файла.
    # ICMP у ККТ может быть закрыт; фактической проверкой будет SSH ниже.
    if [ "$ppp_start_status" -eq 2 ]; then
        echo "PPP reports an existing channel; validating it with KKT SSH." >&2
        ppp_started=1
    else
        echo "PPP start failed with exit status $ppp_start_status; diagnostics:" >&2
        if [ -r {PPP_LOG_FILE} ]; then
            echo "--- {PPP_LOG_FILE} ---" >&2
            tail -n 100 {PPP_LOG_FILE} >&2 || true
        else
            echo "PPP log is absent: {PPP_LOG_FILE}" >&2
        fi
        if command -v journalctl >/dev/null 2>&1; then
            echo "--- recent ukm journal records ---" >&2
            journalctl --no-pager -t ukm -n 100 >&2 || true
        fi
        for system_log in /var/log/messages /var/log/syslog /var/log/daemon.log; do
            if [ -r "$system_log" ]; then
                echo "--- $system_log (ppp-pos2kkt) ---" >&2
                grep -F 'ppp-pos2kkt' "$system_log" | tail -n 100 >&2 || true
            fi
        done
        exit "$ppp_start_status"
    fi
fi
time_value="$(date +%m%d%H%M%Y)"
ssh -i {KKT_SSH_KEY} -p {KKT_SSH_PORT} -o BatchMode=yes -o ConnectTimeout=10 {KKT_SSH_USER}@{PPP_KKT_IP} "sudo -n date $time_value"
{stop_ppp}
ppp_started=0
/etc/init.d/ukmclient start
ukmclient_stopped=0
trap - EXIT HUP INT TERM'''


def sync_kkt_time(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    """Передать на ККТ текущие дату и время кассы через PPP."""
    step = KKT_TIME_STEPS[0]
    if progress is not None:
        progress(step, "running", None)
    logger.info("Starting KKT time synchronization on %s", config.connection.host)
    try:
        with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
            # PPP-скрипты используют terminal utilities; без PTY они завершаются с TERM unset.
            remote.run(build_sync_command(), use_sudo=True, timeout=90, get_pty=True)
    except Exception as exc:
        if progress is not None:
            progress(step, "error", str(exc))
        raise
    if progress is not None:
        progress(step, "success", "Время ККТ синхронизировано")
    logger.info("KKT time synchronization completed on %s", config.connection.host)
