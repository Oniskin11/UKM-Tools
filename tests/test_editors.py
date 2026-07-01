from __future__ import annotations

import unittest

from dbrepair.editors import set_innodb_force_recovery, update_db_ini


class UpdateDbIniTests(unittest.TestCase):
    def test_replaces_existing_values(self) -> None:
        source = "export DBNAME=old\nexport DBPASSWORD=bad\n"
        result = update_db_ini(source, "ukmclient", "CtHDbCGK.C")
        self.assertEqual(result, "export DBNAME=ukmclient\nexport DBPASSWORD=CtHDbCGK.C\n")

    def test_appends_missing_values(self) -> None:
        source = "# config\n"
        result = update_db_ini(source, "ukmclient", "CtHDbCGK.C")
        self.assertEqual(
            result,
            "# config\nexport DBNAME=ukmclient\nexport DBPASSWORD=CtHDbCGK.C\n",
        )


class MyCnfEditTests(unittest.TestCase):
    def test_enables_force_recovery(self) -> None:
        source = "[mysqld]\nuser=mysql\n#set-variable=innodb_force_recovery=6\n"
        result = set_innodb_force_recovery(source, enabled=True)
        self.assertEqual(
            result,
            "[mysqld]\nuser=mysql\nset-variable=innodb_force_recovery=6\n",
        )

    def test_disables_force_recovery(self) -> None:
        source = "[mysqld]\nset-variable=innodb_force_recovery=6\n"
        result = set_innodb_force_recovery(source, enabled=False)
        self.assertEqual(result, "[mysqld]\n#set-variable=innodb_force_recovery=6\n")

    def test_adds_line_when_missing(self) -> None:
        source = "[mysqld]\nuser=mysql\n[mysqld_safe]\n"
        result = set_innodb_force_recovery(source, enabled=True)
        self.assertEqual(
            result,
            "[mysqld]\nuser=mysql\nset-variable=innodb_force_recovery=6\n[mysqld_safe]\n",
        )

    def test_fails_without_mysqld_section(self) -> None:
        source = "[client]\nuser=root\n"
        with self.assertRaises(ValueError):
            set_innodb_force_recovery(source, enabled=True)


if __name__ == "__main__":
    unittest.main()
