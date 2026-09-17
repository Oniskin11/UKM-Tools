from __future__ import annotations

import unittest

from dbrepair.localization import localize_message


class LocalizationTests(unittest.TestCase):
    def test_translates_log_labels_and_connection_message(self) -> None:
        self.assertEqual(
            localize_message("SSH connection established to 192.168.1.10:22"),
            "Установлено SSH-подключение к 192.168.1.10:22",
        )
        self.assertEqual(
            localize_message("RUN /bin/sh -lc 'date'"),
            "Выполнение команды: /bin/sh -lc 'date'",
        )

    def test_translates_remote_command_error(self) -> None:
        message = "Remote command failed with exit status 1: date\nstdout:\nok\nstderr:\nfailed"
        translated = localize_message(message)

        self.assertIn("Удалённая команда завершилась с кодом 1", translated)
        self.assertIn("стандартный вывод:", translated)
        self.assertIn("стандартный вывод ошибок:", translated)


if __name__ == "__main__":
    unittest.main()
