from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import re
import shlex
import threading
import uuid
from xml.etree import ElementTree

from .config import AppConfig
from .remote import RemoteClient

KKT_SERIAL_PORT = "/dev/ttyS0"
KKT_SERIAL_BAUDRATE = 115200
KKT_SERIAL_RESPONSE_TIMEOUT = 10
KKT_SYNC_TIMEOUT = 45
KKT_PROTOCOL_LABEL = "OFDFNARMUKM"
KKT_PROTOCOL_VERSION = "13.0"
KKT_FFD_VERSION = "4"
KKT_CONTAINER_VERSION = "1"
KKT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_RESPONSE_MARKER = "__DBREPAIR_KKT_RESPONSE__"


@dataclass(frozen=True)
class KktTimeStep:
    step_id: str
    number: str
    title: str


KKT_TIME_STEPS: tuple[KktTimeStep, ...] = (
    KktTimeStep("sync_time", "1", "Синхронизировать время ККТ"),
)

ProgressCallback = Callable[[KktTimeStep, str, str | None], None]


class KktApiError(RuntimeError):
    """The KKT rejected a command or returned an invalid response."""


def build_kkt_request(
    command: int,
    *,
    request_datetime: datetime,
    data: str = "",
    request_id: str | None = None,
) -> str:
    """Build a raw XML request for the documented KKT control protocol."""
    identifier = request_id or str(uuid.uuid4())
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<ArmRequest>
 <RequestBody>
  <ProtocolLabel>{KKT_PROTOCOL_LABEL}</ProtocolLabel>
  <ProtocolVersion>{KKT_PROTOCOL_VERSION}</ProtocolVersion>
  <RequestId>{{{identifier}}}</RequestId>
  <DateTime>{request_datetime.strftime(KKT_DATETIME_FORMAT)}</DateTime>
  <Command>{command}</Command>
  <msgFFDVer>{KKT_FFD_VERSION}</msgFFDVer>
  <msgContVer>{KKT_CONTAINER_VERSION}</msgContVer>
 </RequestBody>
 <RequestData><![CDATA[{data}]]></RequestData>
</ArmRequest>'''


def build_serial_sync_command(cash_time: datetime) -> str:
    """Exchange KKT raw XML through RS-232 and always restore ukmclient."""
    status_request = build_kkt_request(2, request_datetime=cash_time)
    set_data = (
        '<pa n="200001" t="7"><pa n="DateTime" t="5">'
        f"{cash_time.strftime(KKT_DATETIME_FORMAT)}</pa></pa>"
    )
    set_request = build_kkt_request(24, request_datetime=cash_time, data=set_data)
    verify_request = build_kkt_request(22, request_datetime=cash_time)
    script = f'''set -eu
serial_port={shlex.quote(KKT_SERIAL_PORT)}
serial_state=""
ukmclient_stopped=0
serial_open=0
cleanup() {{
    if [ "$serial_open" -eq 1 ]; then
        exec 3>&- || true
        exec 3<&- || true
    fi
    if [ -n "$serial_state" ]; then
        stty -F "$serial_port" "$serial_state" || true
    fi
    if [ "$ukmclient_stopped" -eq 1 ]; then
        /etc/init.d/ukmclient start || true
    fi
}}
trap cleanup EXIT HUP INT TERM
status_request={shlex.quote(status_request)}
set_request={shlex.quote(set_request)}
verify_request={shlex.quote(verify_request)}
/etc/init.d/ukmclient stop
ukmclient_stopped=1
sleep 1
serial_state="$(stty -F "$serial_port" -g)"
stty -F "$serial_port" {KKT_SERIAL_BAUDRATE} cs8 -cstopb -parenb -ixon -ixoff -crtscts -icanon -isig -iexten -echo min 1 time 0
exec 3<>"$serial_port"
serial_open=1
exchange() {{
    request="$1"
    response=""
    char=""
    printf '%s' "$request" >&3
    while :; do
        char=""
        if ! IFS= read -r -n 1 -t {KKT_SERIAL_RESPONSE_TIMEOUT} char <&3; then
            echo "Истекло время ожидания ответа ККТ на $serial_port" >&2
            return 1
        fi
        response="$response$char"
        case "$response" in
            *'</ArmResponse>')
                printf '%s' "$response"
                return 0
                ;;
        esac
    done
}}
status_response="$(exchange "$status_request")"
if [[ "$status_response" != *'<Result>0</Result>'* ]] || [[ "$status_response" != *'<Command>3</Command>'* ]]; then
    echo "Команда GetStatus ККТ завершилась ошибкой: $status_response" >&2
    exit 1
fi
if [[ "$status_response" != *'n="ShiftState" t="4">0</pa>'* ]]; then
    echo "Смена ККТ открыта; дату и время изменить нельзя." >&2
    exit 1
fi
set_response="$(exchange "$set_request")"
if [[ "$set_response" != *'<Result>0</Result>'* ]] || [[ "$set_response" != *'<Command>25</Command>'* ]]; then
    echo "Команда DateTimeSet ККТ завершилась ошибкой: $set_response" >&2
    exit 1
fi
verify_response="$(exchange "$verify_request")"
printf '%s\\n%s\\n' '{_RESPONSE_MARKER}' "$verify_response"'''
    return f"bash -c {shlex.quote(script)}"


def _cash_datetime(remote: RemoteClient) -> datetime:
    result = remote.run("date '+%Y-%m-%d %H:%M:%S'", timeout=10)
    try:
        return datetime.strptime(result.stdout.strip(), KKT_DATETIME_FORMAT)
    except ValueError as exc:
        raise RuntimeError(f"Касса вернула некорректное текущее время: {result.stdout!r}") from exc


def _extract_response(output: str) -> ElementTree.Element:
    marker_index = output.find(_RESPONSE_MARKER)
    if marker_index < 0:
        raise KktApiError("Обмен с ККТ по последовательному порту не вернул ответ проверки.")
    xml_start = output.find("<ArmResponse>", marker_index)
    xml_end = output.find("</ArmResponse>", xml_start)
    if xml_start < 0 or xml_end < 0:
        raise KktApiError("ККТ вернула некорректный XML.")
    try:
        return ElementTree.fromstring(output[xml_start : xml_end + len("</ArmResponse>")])
    except ElementTree.ParseError as exc:
        raise KktApiError("ККТ вернула некорректный XML.") from exc


def _response_value(response: ElementTree.Element, name: str) -> str | None:
    data = response.findtext("./ResponseData") or ""
    match = re.search(
        rf'<pa\s+[^>]*n="{re.escape(name)}"[^>]*>(.*?)</pa>',
        data,
        flags=re.DOTALL,
    )
    return match.group(1).strip() if match else None


def _verified_kkt_datetime(output: str) -> datetime:
    response = _extract_response(output)
    result = response.findtext("./ResponseBody/Result")
    command = response.findtext("./ResponseBody/Command")
    if result != "0" or command != "23":
        raise KktApiError(
            f"Команда DateTimeGet ККТ завершилась ошибкой: result={result!r}, command={command!r}."
        )
    value = _response_value(response, "DateTime")
    try:
        return datetime.strptime(value, KKT_DATETIME_FORMAT)
    except (TypeError, ValueError) as exc:
        raise KktApiError(f"ККТ вернула некорректные дату и время: {value!r}") from exc


def sync_kkt_time(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    """Synchronize KKT time through its documented raw RS-232 XML API."""
    step = KKT_TIME_STEPS[0]
    if progress is not None:
        progress(step, "running", None)
    logger.info("Starting KKT time synchronization on %s via RS-232 API", config.connection.host)
    try:
        with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
            cash_time = _cash_datetime(remote)
            result = remote.run(
                build_serial_sync_command(cash_time),
                use_sudo=True,
                timeout=KKT_SYNC_TIMEOUT,
                get_pty=False,
            )
            kkt_time = _verified_kkt_datetime(result.stdout)
            if abs((kkt_time - cash_time).total_seconds()) > KKT_SERIAL_RESPONSE_TIMEOUT:
                raise KktApiError(
                    f"Проверка времени ККТ не пройдена: ожидалось {cash_time}, получено {kkt_time}."
                )
    except Exception as exc:
        if progress is not None:
            progress(step, "error", str(exc))
        raise
    if progress is not None:
        progress(step, "success", "Время ККТ синхронизировано через RS-232 API")
    logger.info("KKT time synchronization completed on %s via RS-232 API", config.connection.host)
