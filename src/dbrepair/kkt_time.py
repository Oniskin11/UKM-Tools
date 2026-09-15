from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import re
import threading
from typing import Any
import uuid
from xml.etree import ElementTree

from .config import AppConfig
from .remote import OperationCancelledError, RemoteClient

PPP_KKT_IP = "192.168.250.2"
KKT_API_PORT = 6667
KKT_API_TIMEOUT = 15
KKT_PROTOCOL_LABEL = "OFDFNARMUKM"
KKT_PROTOCOL_VERSION = "13.0"
KKT_FFD_VERSION = "4"
KKT_CONTAINER_VERSION = "1"
KKT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_RESPONSE_END = b"</ArmResponse>"
_MAX_RESPONSE_BYTES = 1_048_576


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
    """The KKT accepted the connection but rejected an API request."""


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


class KktApiClient:
    def __init__(
        self,
        remote: RemoteClient,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.remote = remote
        self.cancel_event = cancel_event

    def get_status(self, request_datetime: datetime) -> int:
        response = self._request(2, 3, request_datetime=request_datetime)
        shift_state = self._response_value(response, "ShiftState")
        try:
            return int(shift_state)
        except (TypeError, ValueError) as exc:
            raise KktApiError("KKT response does not contain a valid ShiftState.") from exc

    def set_datetime(self, value: datetime) -> None:
        data = f'<pa n="200001" t="7"><pa n="DateTime" t="5">{value.strftime(KKT_DATETIME_FORMAT)}</pa></pa>'
        self._request(24, 25, request_datetime=value, data=data)

    def get_datetime(self, request_datetime: datetime) -> datetime:
        response = self._request(22, 23, request_datetime=request_datetime)
        value = self._response_value(response, "DateTime")
        try:
            return datetime.strptime(value, KKT_DATETIME_FORMAT)
        except (TypeError, ValueError) as exc:
            raise KktApiError(f"KKT returned an invalid date and time: {value!r}") from exc

    def _request(
        self,
        command: int,
        response_command: int,
        *,
        request_datetime: datetime,
        data: str = "",
    ) -> ElementTree.Element:
        self._raise_if_cancelled()
        payload = build_kkt_request(
            command,
            request_datetime=request_datetime,
            data=data,
        ).encode("utf-8")
        channel = self.remote.open_tcp_channel(
            PPP_KKT_IP,
            KKT_API_PORT,
            timeout=KKT_API_TIMEOUT,
        )
        try:
            channel.settimeout(KKT_API_TIMEOUT)
            channel.sendall(payload)
            response = bytearray()
            while _RESPONSE_END not in response:
                self._raise_if_cancelled(channel)
                chunk = channel.recv(4096)
                if not chunk:
                    raise KktApiError("KKT closed the API connection before sending a response.")
                response.extend(chunk)
                if len(response) > _MAX_RESPONSE_BYTES:
                    raise KktApiError("KKT API response exceeds the allowed size.")
        except OperationCancelledError:
            raise
        except KktApiError:
            raise
        except Exception as exc:
            raise KktApiError(f"KKT API command {command} failed: {exc}") from exc
        finally:
            channel.close()

        try:
            root = ElementTree.fromstring(bytes(response))
        except ElementTree.ParseError as exc:
            raise KktApiError("KKT returned malformed XML.") from exc
        result = root.findtext("./ResponseBody/Result")
        error_code = root.findtext("./ResponseBody/ErrorCode")
        error_description = root.findtext("./ResponseBody/ErrorDescription") or ""
        actual_command = root.findtext("./ResponseBody/Command")
        if result != "0":
            raise KktApiError(
                f"KKT API command {command} was rejected: "
                f"result={result!r}, error={error_code!r} {error_description.strip()}"
            )
        if actual_command != str(response_command):
            raise KktApiError(
                f"KKT API command {command} returned unexpected command {actual_command!r}."
            )
        return root

    @staticmethod
    def _response_value(response: ElementTree.Element, name: str) -> str | None:
        data = response.findtext("./ResponseData") or ""
        match = re.search(
            rf'<pa\s+[^>]*n="{re.escape(name)}"[^>]*>(.*?)</pa>',
            data,
            flags=re.DOTALL,
        )
        return match.group(1).strip() if match else None

    def _raise_if_cancelled(self, channel: Any | None = None) -> None:
        if self.cancel_event is None or not self.cancel_event.is_set():
            return
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        raise OperationCancelledError("Operation cancelled by user.")


def _cash_datetime(remote: RemoteClient) -> datetime:
    result = remote.run("date '+%Y-%m-%d %H:%M:%S'", timeout=10)
    try:
        return datetime.strptime(result.stdout.strip(), KKT_DATETIME_FORMAT)
    except ValueError as exc:
        raise RuntimeError(f"Cash node returned an invalid current time: {result.stdout!r}") from exc


def sync_kkt_time(
    config: AppConfig,
    logger,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    """Synchronize KKT time through its documented TCP/XML API."""
    step = KKT_TIME_STEPS[0]
    if progress is not None:
        progress(step, "running", None)
    logger.info("Starting KKT time synchronization on %s via API", config.connection.host)
    try:
        with RemoteClient(config.connection, logger, cancel_event=cancel_event) as remote:
            cash_time = _cash_datetime(remote)
            api = KktApiClient(remote, cancel_event=cancel_event)
            shift_state = api.get_status(cash_time)
            if shift_state != 0:
                raise KktApiError(
                    "KKT shift is open; the API forbids changing date and time until it is closed."
                )
            api.set_datetime(cash_time)
            kkt_time = api.get_datetime(cash_time)
            if abs((kkt_time - cash_time).total_seconds()) > KKT_API_TIMEOUT:
                raise KktApiError(
                    f"KKT time verification failed: expected {cash_time}, received {kkt_time}."
                )
    except Exception as exc:
        if progress is not None:
            progress(step, "error", str(exc))
        raise
    if progress is not None:
        progress(step, "success", "Время ККТ синхронизировано через API")
    logger.info("KKT time synchronization completed on %s via API", config.connection.host)
