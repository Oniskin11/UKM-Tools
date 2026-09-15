from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dbrepair.kkt_time import build_sync_command, sync_kkt_time


class KktTimeCommandTests(unittest.TestCase):
    def test_uses_fixed_ppp_parameters_without_config(self) -> None:
        command = build_sync_command()
        self.assertIn("192.168.250.1", command)
        self.assertIn("192.168.250.2", command)
        self.assertIn("/dev/ttyS0", command)
        self.assertIn("sh -x ./ppp-pos2kkt-start.sh", command)
        self.assertIn("sh -x ./ppp-pos2kkt-stop.sh", command)
        self.assertNotIn("config.toml", command)

    def test_is_noninteractive_and_recovers_service(self) -> None:
        command = build_sync_command()
        self.assertIn("-o BatchMode=yes", command)
        self.assertIn("sudo -n date", command)
        self.assertIn("trap cleanup EXIT HUP INT TERM", command)
        self.assertIn("/etc/init.d/ukmclient start || true", command)
        self.assertIn("PPP start failed with exit status", command)
        self.assertIn('ping -c 1 -W 2 192.168.250.2', command)
        self.assertIn("PPP is already active; continuing", command)
        self.assertIn("journalctl --no-pager -t ukm -n 100", command)
        self.assertIn("/var/log/messages /var/log/syslog /var/log/daemon.log", command)

    def test_sets_date_from_cash_clock(self) -> None:
        command = build_sync_command()
        self.assertIn('time_value="$(date +%m%d%H%M%Y)"', command)
        self.assertIn('"sudo -n date $time_value"', command)

    def test_requests_terminal_for_ppp_scripts(self) -> None:
        calls: list[dict] = []

        class Remote:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                pass

            def run(self, _command, **kwargs) -> None:
                calls.append(kwargs)

        config = SimpleNamespace(connection=SimpleNamespace(host="cash"))
        logger = SimpleNamespace(info=lambda *_args: None)
        with patch("dbrepair.kkt_time.RemoteClient", Remote):
            sync_kkt_time(config, logger)

        self.assertEqual(calls, [{"use_sudo": True, "timeout": 90, "get_pty": True}])


if __name__ == "__main__":
    unittest.main()
