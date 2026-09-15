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
{start_ppp}
ppp_started=1
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
            remote.run(build_sync_command(), use_sudo=True, timeout=90)
    except Exception as exc:
        if progress is not None:
            progress(step, "error", str(exc))
        raise
    if progress is not None:
        progress(step, "success", "Время ККТ синхронизировано")
    logger.info("KKT time synchronization completed on %s", config.connection.host)
