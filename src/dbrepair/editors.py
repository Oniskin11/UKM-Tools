from __future__ import annotations

import re


DBNAME_PATTERN = re.compile(r"^\s*export\s+DBNAME=.*$")
DBPASSWORD_PATTERN = re.compile(r"^\s*export\s+DBPASSWORD=.*$")
RECOVERY_LINE = "set-variable=innodb_force_recovery=6"


def update_db_ini(content: str, db_name: str, db_password: str) -> str:
    lines, newline = _split_lines(content)
    updated: list[str] = []
    has_dbname = False
    has_dbpassword = False

    for line in lines:
        if DBNAME_PATTERN.match(line):
            updated.append(f"export DBNAME={db_name}")
            has_dbname = True
            continue
        if DBPASSWORD_PATTERN.match(line):
            updated.append(f"export DBPASSWORD={db_password}")
            has_dbpassword = True
            continue
        updated.append(line)

    if not has_dbname:
        updated.append(f"export DBNAME={db_name}")
    if not has_dbpassword:
        updated.append(f"export DBPASSWORD={db_password}")

    return newline.join(updated).rstrip("\r\n") + newline


def set_innodb_force_recovery(content: str, enabled: bool) -> str:
    lines, newline = _split_lines(content)
    updated: list[str] = []
    in_mysqld = False
    touched = False

    for line in lines:
        stripped = line.strip()
        is_section = stripped.startswith("[") and stripped.endswith("]")

        if is_section and in_mysqld and enabled and not touched:
            updated.append(RECOVERY_LINE)
            touched = True

        if stripped == "[mysqld]":
            in_mysqld = True
            updated.append(line)
            continue

        if is_section and stripped != "[mysqld]":
            in_mysqld = False
            updated.append(line)
            continue

        if in_mysqld and _is_recovery_line(stripped):
            updated.append(RECOVERY_LINE if enabled else f"#{RECOVERY_LINE}")
            touched = True
            continue

        updated.append(line)

    if in_mysqld and enabled and not touched:
        updated.append(RECOVERY_LINE)
        touched = True

    if enabled and not touched:
        raise ValueError("Section [mysqld] not found in /etc/my.cnf")

    return newline.join(updated).rstrip("\r\n") + newline


def _is_recovery_line(stripped: str) -> bool:
    if stripped == RECOVERY_LINE:
        return True
    if stripped.startswith("#"):
        return stripped[1:].strip() == RECOVERY_LINE
    return False


def _split_lines(content: str) -> tuple[list[str], str]:
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    if not lines:
        return [], newline
    return lines, newline
