from __future__ import annotations

import unittest

from dbrepair.gui import DbRepairGui, expand_safe_step_chain


class ExpandSafeStepChainTests(unittest.TestCase):
    def test_keeps_regular_step_as_single_step(self) -> None:
        self.assertEqual(expand_safe_step_chain("dump_db"), ("dump_db",))

    def test_expands_replace_datadir_into_safe_chain(self) -> None:
        self.assertEqual(
            expand_safe_step_chain("replace_datadir"),
            ("replace_datadir", "restore_db", "start_ukmclient"),
        )


class EnsureConfigExistsTests(unittest.TestCase):
    def test_creates_default_config_for_missing_path(self) -> None:
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            gui = object.__new__(DbRepairGui)
            gui.config_path_var = Var(str(path))
            gui._ensure_config_exists()

            self.assertTrue(path.is_file())
            self.assertIn("[connection]", path.read_text(encoding="utf-8"))

    def test_expands_restore_into_safe_chain(self) -> None:
        self.assertEqual(
            expand_safe_step_chain("restore_db"),
            ("restore_db", "start_ukmclient"),
        )


if __name__ == "__main__":
    unittest.main()
