from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dbrepair.kkt_time import KktApiClient, KktApiError, build_kkt_request, sync_kkt_time


def _response(command: int, data: str = "", *, result: int = 0) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<ArmResponse><ResponseBody><Result>{result}</Result><ErrorCode>0</ErrorCode>
<ErrorDescription></ErrorDescription><Command>{command}</Command></ResponseBody>
<ResponseData><![CDATA[{data}]]></ResponseData></ArmResponse>'''.encode()


class Channel:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, payload: bytes) -> None:
        self.sent = payload

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response

    def close(self) -> None:
        self.closed = True


class KktTimeApiTests(unittest.TestCase):
    def test_builds_documented_raw_xml_request(self) -> None:
        request = build_kkt_request(
            24,
            request_datetime=datetime(2026, 9, 15, 19, 30, 0),
            data='<pa n="200001" t="7"/>',
            request_id="request-id",
        )

        self.assertIn("<ProtocolLabel>OFDFNARMUKM</ProtocolLabel>", request)
        self.assertIn("<ProtocolVersion>13.0</ProtocolVersion>", request)
        self.assertIn("<RequestId>{request-id}</RequestId>", request)
        self.assertIn("<DateTime>2026-09-15 19:30:00</DateTime>", request)
        self.assertIn("<Command>24</Command>", request)
        self.assertIn("<msgFFDVer>4</msgFFDVer>", request)
        self.assertIn('<![CDATA[<pa n="200001" t="7"/>]]>', request)

    def test_uses_tcp_api_and_verifies_time(self) -> None:
        channels = [
            Channel(_response(3, '<pa n="200000" t="7"><pa n="ShiftState" t="4">0</pa></pa>')),
            Channel(_response(25)),
            Channel(_response(23, '<pa n="200001" t="7"><pa n="DateTime" t="5">2026-09-15 19:30:03</pa></pa>')),
        ]
        calls: list[tuple[str, int, float]] = []
        used_channels: list[Channel] = []

        class Remote:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                pass

            def run(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs
                return SimpleNamespace(stdout="2026-09-15 19:30:00\n")

            def open_tcp_channel(self, host, port, *, timeout):
                calls.append((host, port, timeout))
                channel = channels.pop(0)
                used_channels.append(channel)
                return channel

        config = SimpleNamespace(connection=SimpleNamespace(host="cash"))
        logger = SimpleNamespace(info=lambda *_args: None)
        with patch("dbrepair.kkt_time.RemoteClient", Remote):
            sync_kkt_time(config, logger)

        self.assertEqual(calls, [("192.168.250.2", 6667, 15)] * 3)
        self.assertEqual(
            [b"<Command>2</Command>", b"<Command>24</Command>", b"<Command>22</Command>"],
            [
                next(line.strip() for line in channel.sent.splitlines() if b"<Command>" in line)
                for channel in used_channels
            ],
        )
        self.assertIn(b'<pa n="DateTime" t="5">2026-09-15 19:30:00</pa>', used_channels[1].sent)
        self.assertTrue(all(channel.closed for channel in used_channels))

    def test_refuses_to_change_time_when_shift_is_open(self) -> None:
        channel = Channel(_response(3, '<pa n="200000" t="7"><pa n="ShiftState" t="4">1</pa></pa>'))

        class Remote:
            def open_tcp_channel(self, *_args, **_kwargs):
                return channel

        api = KktApiClient(Remote())
        with self.assertRaisesRegex(KktApiError, "open"):
            shift_state = api.get_status(datetime(2026, 9, 15, 19, 30, 0))
            if shift_state != 0:
                raise KktApiError("KKT shift is open")


if __name__ == "__main__":
    unittest.main()
