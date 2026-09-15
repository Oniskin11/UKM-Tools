from __future__ import annotations

import unittest

from dbrepair.kkt_time import build_sync_command


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

    def test_sets_date_from_cash_clock(self) -> None:
        command = build_sync_command()
        self.assertIn('time_value="$(date +%m%d%H%M%Y)"', command)
        self.assertIn('"sudo -n date $time_value"', command)


if __name__ == "__main__":
    unittest.main()
