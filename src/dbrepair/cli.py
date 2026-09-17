from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config, override_host
from .distsource import DistError, build_source
from .logging_utils import configure_logger
from .localization import localize_message
from .tspiot import TsPiotInstaller
from .workflow import DbRepairWorkflow, WorkflowError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Восстановление БД и обслуживание кассы по SSH/SFTP."
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Путь к TOML-файлу конфигурации. По умолчанию: ./config.toml",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Необязательный путь к журналу. По умолчанию: logs/dbrepair-ГГГГММДД-ЧЧММСС.log",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Запустить графический интерфейс вместо консольного режима.",
    )
    parser.add_argument(
        "--tspiot",
        action="store_true",
        help="Установить модуль ТС ПИоТ (с веб-сервера) вместо восстановления БД.",
    )
    parser.add_argument(
        "--hosts",
        default=None,
        help="Список касс через запятую (по умолчанию берётся connection.host из конфига).",
    )
    return parser


def _run_tspiot(args) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {localize_message(str(exc))}", file=sys.stderr)
        return 2
    if config.tspiot is None:
        print("Ошибка конфигурации: отсутствует секция [tspiot].", file=sys.stderr)
        return 2
    try:
        source = build_source(config)
    except DistError as exc:
        print(f"Ошибка конфигурации: {localize_message(str(exc))}", file=sys.stderr)
        return 2

    logger, log_path = configure_logger(
        logger_name="dbrepair.tspiot.cli",
        log_file=args.log_file,
        default_prefix="dbrepair-tspiot",
    )

    hosts = [h.strip() for h in (args.hosts or config.connection.host).split(",") if h.strip()]

    def progress(step, status, details):
        if status in {"success", "error"}:
            suffix = f" ({details})" if details else ""
            status_label = "Успех" if status == "success" else "Ошибка"
            print(f"  [{step.number}] {step.title}: {status_label}{suffix}")

    failures = 0
    for host in hosts:
        print(f"== {host} ==")
        host_config = override_host(config, host)
        try:
            installer = TsPiotInstaller(
                host_config,
                logger,
                source=source,
                architecture="x64",
                target_base=config.tspiot.target_base,
                auto_detect=True,
            )
            installer.run(progress=progress)
            print(f"  {host}: Успех")
        except (WorkflowError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            failures += 1
            logger.exception("TS PIoT installation failed for %s.", host)
            print(f"  {host}: Ошибка — {localize_message(str(exc))}", file=sys.stderr)

    print(f"Подробный журнал: {log_path}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from .gui import launch_gui

        launch_gui(config_path=args.config)
        return 0

    if args.tspiot:
        return _run_tspiot(args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {localize_message(str(exc))}", file=sys.stderr)
        return 2

    logger, log_path = configure_logger(
        logger_name="dbrepair.cli",
        log_file=args.log_file,
        default_prefix="dbrepair",
    )
    logger.info("Using config %s", config.source_path)

    try:
        artifacts = DbRepairWorkflow(config, logger).run()
    except (WorkflowError, RuntimeError, OSError, TimeoutError) as exc:
        logger.exception("Workflow failed.")
        print(f"Восстановление БД завершилось с ошибкой: {localize_message(str(exc))}", file=sys.stderr)
        print(f"Подробный журнал: {log_path}", file=sys.stderr)
        return 1

    print("Восстановление БД успешно завершено.")
    print(f"Локальная копия SQL: {artifacts.local_dump_copy}")
    print(f"Копия SQL на кассе: {artifacts.remote_dump_copy}")
    print(f"Резервная копия datadir MySQL на кассе: {artifacts.remote_mysql_backup}")
    print(f"Подробный журнал: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
