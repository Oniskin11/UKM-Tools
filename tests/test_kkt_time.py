from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dbrepair.kkt_time import (
    KktApiError,
    _verified_kkt_datetime,
    build_kkt_request,
    build_serial_sync_command,
    sync_kkt_time,
)


def _response(command: int, data: str = "", *, result: int = 0) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<ArmResponse><ResponseBody><Result>{result}</Result><ErrorCode>0</ErrorCode>
<ErrorDescription></ErrorDescription><Command>{command}</Command></ResponseBody>
<ResponseData><![CDATA[{data}]]></ResponseData></ArmResponse>'''


class KktTimeSerialTests(unittest.TestCase):
    def test_builds_documented_raw_xml_request(self) -> None:
        request = build_kkt_request(
            24,
            request_datetime=datetime(2026, 9, 16, 0, 10, 0),
            data='<pa n="200001" t="7"/>',
            request_id="request-id",
        )

        self.assertIn("<ProtocolLabel>OFDFNARMUKM</ProtocolLabel>", request)
        self.assertIn("<ProtocolVersion>13.0</ProtocolVersion>", request)
        self.assertIn("<RequestId>{request-id}</RequestId>", request)
        self.assertIn("<DateTime>2026-09-16 00:10:00</DateTime>", request)
        self.assertIn("<Command>24</Command>", request)
        self.assertIn("<msgFFDVer>4</msgFFDVer>", request)

    def test_serial_command_uses_raw_rs232_and_restores_service(self) -> None:
        command = build_serial_sync_command(datetime(2026, 9, 16, 0, 10, 0))

        self.assertIn("/dev/ttyS0", command)
        self.assertIn("115200 cs8 -cstopb -parenb", command)
        self.assertIn("-ixon -ixoff -crtscts", command)
        self.assertIn("/etc/init.d/ukmclient stop", command)
        self.assertIn("/etc/init.d/ukmclient start || true", command)
        self.assertIn("trap cleanup EXIT HUP INT TERM", command)
        self.assertIn("read -r -n 1 -t 10", command)
        self.assertIn("<Command>2</Command>", command)
        self.assertIn("<Command>24</Command>", command)
        self.assertIn("<Command>22</Command>", command)
        self.assertNotIn("ppp-pos2kkt", command)
        self.assertNotIn("192.168.250.2", command)

    def test_verifies_datetime_response(self) -> None:
        output = "diagnostic\n__DBREPAIR_KKT_RESPONSE__\n" + _response(
            23,
            '<pa n="200001" t="7"><pa n="DateTime" t="5">2026-09-16 00:10:03</pa></pa>',
        )

        self.assertEqual(
            _verified_kkt_datetime(output),
            datetime(2026, 9, 16, 0, 10, 3),
        )

    def test_rejects_failed_verification_response(self) -> None:
        output = "__DBREPAIR_KKT_RESPONSE__\n" + _response(23, result=1)

        with self.assertRaisesRegex(KktApiError, "DateTimeGet failed"):
            _verified_kkt_datetime(output)

    def test_uses_one_privileged_serial_exchange(self) -> None:
        calls: list[tuple[str, dict]] = []
        output = "__DBREPAIR_KKT_RESPONSE__\n" + _response(
            23,
            '<pa n="200001" t="7"><pa n="DateTime" t="5">2026-09-16 00:10:03</pa></pa>',
        )

        class Remote:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                pass

            def run(self, command, **kwargs):
                calls.append((command, kwargs))
                if command.startswith("date "):
                    return SimpleNamespace(stdout="2026-09-16 00:10:00\n")
                return SimpleNamespace(stdout=output)

        config = SimpleNamespace(connection=SimpleNamespace(host="cash"))
        logger = SimpleNamespace(info=lambda *_args: None)
        with patch("dbrepair.kkt_time.RemoteClient", Remote):
            sync_kkt_time(config, logger)

        self.assertEqual(calls[0], ("date '+%Y-%m-%d %H:%M:%S'", {"timeout": 10}))
        self.assertEqual(
            calls[1][1],
            {"use_sudo": True, "timeout": 45, "get_pty": False},
        )


if __name__ == "__main__":
    unittest.main()
