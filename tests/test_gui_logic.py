from __future__ import annotations

import unittest

from dbrepair.gui import expand_safe_step_chain


class ExpandSafeStepChainTests(unittest.TestCase):
    def test_keeps_regular_step_as_single_step(self) -> None:
        self.assertEqual(expand_safe_step_chain("dump_db"), ("dump_db",))

    def test_expands_replace_datadir_into_safe_chain(self) -> None:
        self.assertEqual(
            expand_safe_step_chain("replace_datadir"),
            ("replace_datadir", "restore_db", "start_ukmclient"),
        )

    def test_expands_restore_into_safe_chain(self) -> None:
        self.assertEqual(
            expand_safe_step_chain("restore_db"),
            ("restore_db", "start_ukmclient"),
        )


if __name__ == "__main__":
    unittest.main()
