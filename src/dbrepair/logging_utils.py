from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import copy
import logging
import sys

from .localization import localize_message


class RussianFormatter(logging.Formatter):
    """Переводит операторские сообщения, оставляя команду и её вывод читаемыми."""

    def format(self, record: logging.LogRecord) -> str:
        localized = copy.copy(record)
        localized.msg = localize_message(record.getMessage())
        localized.args = ()
        rendered = super().format(localized)
        return "\n".join(localize_message(line) for line in rendered.splitlines())


def configure_logger(
    *,
    logger_name: str,
    log_file: str | Path | None = None,
    default_prefix: str = "dbrepair",
    include_stream: bool = True,
    extra_handlers: Sequence[logging.Handler] | None = None,
) -> tuple[logging.Logger, Path]:
    if log_file is None:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{default_prefix}-{_timestamp()}.log"
    else:
        log_path = Path(log_file).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = RussianFormatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: list[logging.Handler] = []
    if include_stream:
        handlers.append(logging.StreamHandler(sys.stdout))
    handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    if extra_handlers:
        handlers.extend(extra_handlers)

    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger, log_path


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")
