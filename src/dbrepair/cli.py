from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config, override_host
from .logging_utils import configure_logger
from .tspiot import TsPiotInstaller
from .workflow import DbRepairWorkflow, WorkflowError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automate DB repair workflow on a remote cash register over SSH/SFTP."
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to TOML config file. Default: ./config.toml",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to log file. Default: logs/dbrepair-YYYYMMDD-HHMMSS.log",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the Tkinter GUI instead of the console workflow.",
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
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if config.tspiot is None:
        print("Config error: отсутствует секция [tspiot].", file=sys.stderr)
        return 2
    if config.webserver is None:
        print("Config error: отсутствует секция [webserver] (base_url).", file=sys.stderr)
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
            print(f"  [{step.number}] {step.title}: {status}{suffix}")

    failures = 0
    for host in hosts:
        print(f"== {host} ==")
        host_config = override_host(config, host)
        try:
            installer = TsPiotInstaller(
                host_config,
                logger,
                base_url=config.webserver.normalized(),
                architecture="x64",
                target_base=config.tspiot.target_base,
                auto_detect=True,
            )
            installer.run(progress=progress)
            print(f"  {host}: OK")
        except (WorkflowError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            failures += 1
            logger.exception("TS PIoT installation failed for %s.", host)
            print(f"  {host}: FAIL — {exc}", file=sys.stderr)

    print(f"Detailed log: {log_path}")
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
        print(f"Config error: {exc}", file=sys.stderr)
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
        print(f"Workflow failed: {exc}", file=sys.stderr)
        print(f"Detailed log: {log_path}", file=sys.stderr)
        return 1

    print("DB repair completed successfully.")
    print(f"Local SQL backup: {artifacts.local_dump_copy}")
    print(f"Remote SQL backup: {artifacts.remote_dump_copy}")
    print(f"Remote MySQL datadir backup: {artifacts.remote_mysql_backup}")
    print(f"Detailed log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
