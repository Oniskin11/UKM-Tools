from __future__ import annotations

import logging
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .config import ConfigError, load_config, override_host
from .logging_utils import configure_logger
from .remote import OperationCancelledError
from .tspiot import TSPIOT_STEPS, TsPiotInstaller, TsPiotStep, detect_environment, reboot_host
from .workflow import DbRepairWorkflow, WORKFLOW_STEPS, WorkflowArtifacts, WorkflowStep


SAFE_STEP_CHAINS: dict[str, tuple[str, ...]] = {
    "replace_datadir": ("replace_datadir", "restore_db", "start_ukmclient"),
    "restore_db": ("restore_db", "start_ukmclient"),
}


class QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: queue.Queue[tuple], host: str):
        super().__init__()
        self._event_queue = event_queue
        self._host = host

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._event_queue.put(("log", self._host, message))


class TargetView:
    def __init__(self, app: "DbRepairGui", notebook: ttk.Notebook, host: str):
        self.app = app
        self.host = host
        self.host_token = _host_token(host)
        self.frame = ttk.Frame(notebook, padding=12)
        self.summary_var = tk.StringVar(value="Готово к запуску.")
        self.log_path_var = tk.StringVar()
        self.status_vars: dict[str, tk.StringVar] = {}
        self.detail_vars: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self.step_buttons: dict[str, ttk.Button] = {}
        self.busy = False
        self.cancel_event: threading.Event | None = None
        self.current_step_id: str | None = None

        queue_handler = QueueLogHandler(self.app.event_queue, self.host)
        self.logger, log_path = configure_logger(
            logger_name=f"dbrepair.gui.{self.host_token}.{id(self)}",
            default_prefix=f"dbrepair-gui-{self.host_token}",
            include_stream=False,
            extra_handlers=[queue_handler],
        )
        self.log_path_var.set(str(log_path.resolve()))

        self._build_ui()
        self.reset_statuses()
        self.append_log(f"Лог сохраняется в {self.log_path_var.get()}")

    def _build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)

        info_frame = ttk.LabelFrame(self.frame, text="Подключение", padding=12)
        info_frame.grid(row=0, column=0, sticky="ew")
        info_frame.columnconfigure(1, weight=1)

        ttk.Label(info_frame, text="SSH/IP адрес").grid(row=0, column=0, sticky="w", padx=(0, 8))
        host_entry = ttk.Entry(info_frame)
        host_entry.insert(0, self.host)
        host_entry.configure(state="readonly")
        host_entry.grid(row=0, column=1, sticky="ew")

        ttk.Label(info_frame, text="Лог-файл").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Entry(info_frame, textvariable=self.log_path_var, state="readonly").grid(
            row=1,
            column=1,
            sticky="ew",
            pady=(10, 0),
        )

        actions_frame = ttk.Frame(self.frame, padding=(0, 12, 0, 12))
        actions_frame.grid(row=1, column=0, sticky="ew")
        actions_frame.columnconfigure(4, weight=1)

        self.run_all_button = ttk.Button(actions_frame, text="Выполнить все шаги", command=self.run_all)
        self.run_all_button.grid(row=0, column=0, padx=(0, 8))

        self.reset_button = ttk.Button(actions_frame, text="Сбросить статусы", command=self.reset_statuses)
        self.reset_button.grid(row=0, column=1, padx=(0, 8))

        self.cancel_button = ttk.Button(actions_frame, text="Отменить", command=self.cancel)
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))

        ttk.Label(actions_frame, textvariable=self.summary_var).grid(row=0, column=4, sticky="w")

        content = ttk.Frame(self.frame)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        steps_frame = ttk.Frame(content, padding=(12, 12, 12, 0))
        steps_frame.grid(row=0, column=0, sticky="ew")
        steps_frame.columnconfigure(1, weight=1)
        steps_frame.columnconfigure(3, weight=1)

        headers = ("Шаг", "Действие", "Статус", "Комментарий", "")
        for column, title in enumerate(headers):
            ttk.Label(steps_frame, text=title).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 8))

        for index, step in enumerate(WORKFLOW_STEPS, start=1):
            ttk.Label(steps_frame, text=step.number).grid(row=index, column=0, sticky="nw", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, text=step.title).grid(row=index, column=1, sticky="ew", padx=(0, 8), pady=4)

            status_var = tk.StringVar()
            detail_var = tk.StringVar()
            status_label = ttk.Label(steps_frame, textvariable=status_var, style="Pending.TLabel")
            status_label.grid(row=index, column=2, sticky="w", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, textvariable=detail_var, wraplength=320).grid(
                row=index,
                column=3,
                sticky="ew",
                padx=(0, 8),
                pady=4,
            )
            button = ttk.Button(
                steps_frame,
                text="Выполнить",
                command=lambda step_id=step.step_id: self.run_step(step_id),
            )
            button.grid(row=index, column=4, sticky="e", pady=4)

            self.status_vars[step.step_id] = status_var
            self.detail_vars[step.step_id] = detail_var
            self.status_labels[step.step_id] = status_label
            self.step_buttons[step.step_id] = button

        log_frame = ttk.Frame(content, padding=(12, 12, 12, 12))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        ttk.Label(log_frame, text="Журнал выполнения").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log_text = tk.Text(log_frame, wrap="word", height=10, font=("Consolas", 10), state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def run_all(self) -> bool:
        return self.start_worker([step.step_id for step in WORKFLOW_STEPS], reset=True)

    def run_step(self, step_id: str) -> bool:
        effective_step_ids = list(expand_safe_step_chain(step_id))
        if len(effective_step_ids) > 1:
            chain_caption = " -> ".join(_step_number(candidate) for candidate in effective_step_ids)
            self.summary_var.set(f"Для безопасности будет выполнена цепочка шагов {chain_caption}.")
            self.logger.info(
                "Step %s requested; expanding to safe chain: %s",
                step_id,
                ", ".join(effective_step_ids),
            )
        return self.start_worker(effective_step_ids, reset=False)

    def start_worker(self, step_ids: list[str], *, reset: bool) -> bool:
        if self.busy:
            return False
        if reset:
            self.reset_statuses()

        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.summary_var.set("Выполнение...")
        thread = threading.Thread(
            target=self._worker_run_steps,
            args=(step_ids, self.app.config_path_var.get().strip(), self.host, self.cancel_event),
            daemon=True,
        )
        thread.start()
        return True

    def _worker_run_steps(
        self,
        step_ids: list[str],
        config_path: str,
        host: str,
        cancel_event: threading.Event,
    ) -> None:
        progress_seen = False
        current_step_id: str | None = None

        def progress(step: WorkflowStep, status: str, details: str | None) -> None:
            nonlocal progress_seen, current_step_id
            progress_seen = True
            if status == "running":
                current_step_id = step.step_id
            elif status in {"success", "error"}:
                current_step_id = None
            self.app.event_queue.put(("progress", host, step.step_id, status, details))

        try:
            config = load_config(config_path)
            config = override_host(config, host)
            self.logger.info("Using config %s", config.source_path)
            self.logger.info("Target host %s", config.connection.host)

            workflow = DbRepairWorkflow(config, self.logger, cancel_event=cancel_event)
            session = workflow.create_session()
            artifacts = workflow.run_steps(step_ids, session=session, progress=progress)
        except OperationCancelledError as exc:
            self.logger.info("Workflow cancelled by user.")
            self.app.event_queue.put(("task-cancel", host, current_step_id, str(exc)))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Workflow failed.")
            self.app.event_queue.put(("task-error", host, step_ids if not progress_seen else [], str(exc)))
        else:
            self.app.event_queue.put(("task-success", host, step_ids, artifacts))
        finally:
            self.app.event_queue.put(("busy", host, False))

    def handle_progress(self, step_id: str, status: str, details: str | None) -> None:
        if status == "running":
            self.current_step_id = step_id
            self.set_step_state(step_id, "Выполняется", "Running.TLabel", "")
            return
        if status == "success":
            self.current_step_id = None
            self.set_step_state(step_id, "Успех", "Success.TLabel", "Выполнено")
            return
        if status == "error":
            self.current_step_id = None
            self.set_step_state(step_id, "Ошибка", "Error.TLabel", _shorten(details))
            self.summary_var.set("Выполнение остановлено с ошибкой. Подробности в журнале.")

    def handle_success(self, step_ids: list[str], artifacts: WorkflowArtifacts) -> None:
        if len(step_ids) == len(WORKFLOW_STEPS):
            self.summary_var.set(
                f"Готово. SQL копия: {artifacts.local_dump_copy.name}; MySQL backup: {artifacts.remote_mysql_backup}"
            )
            self.current_step_id = None
            return
        if len(step_ids) == 1:
            self.summary_var.set("Шаг успешно выполнен.")
            self.current_step_id = None
            return
        self.summary_var.set("Выбранные шаги успешно выполнены.")
        self.current_step_id = None

    def handle_error(self, step_ids: list[str], message: str) -> None:
        if step_ids:
            for step_id in step_ids:
                self.set_step_state(step_id, "Ошибка", "Error.TLabel", _shorten(message))
        self.summary_var.set(f"Ошибка: {_shorten(message)}")
        self.current_step_id = None

    def handle_cancel(self, step_id: str | None, message: str) -> None:
        if step_id:
            self.set_step_state(step_id, "Отменено", "Cancelled.TLabel", _shorten(message))
        self.summary_var.set("Выполнение отменено пользователем.")
        self.current_step_id = None

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_all_button.configure(state=state)
        self.reset_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        for button in self.step_buttons.values():
            button.configure(state=state)
        if not busy:
            self.cancel_event = None

    def reset_statuses(self) -> None:
        if self.busy:
            return
        for step in WORKFLOW_STEPS:
            self.set_step_state(step.step_id, "Не запускался", "Pending.TLabel", "")
        self.summary_var.set("Готово к запуску.")

    def set_step_state(self, step_id: str, text: str, style: str, detail: str) -> None:
        self.status_vars[step_id].set(text)
        self.detail_vars[step_id].set(detail)
        self.status_labels[step_id].configure(style=style)

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def cancel(self) -> bool:
        if not self.busy or self.cancel_event is None:
            return False
        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        self.summary_var.set("Отмена...")
        self.logger.info("Cancellation requested by user.")
        return True

    def destroy(self) -> None:
        self.cancel()
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
        self.frame.destroy()


class DbRepairGui:
    def __init__(self, root: tk.Tk, config_path: str = "config.toml"):
        self.root = root
        self.root.title("DbRepair")
        self.root.minsize(1280, 760)

        self.event_queue: queue.Queue[tuple] = queue.Queue()
        self.config_path_var = tk.StringVar(value=config_path)
        self.overview_var = tk.StringVar(value="Добавьте адреса касс и создайте вкладки.")
        self.targets: dict[str, TargetView] = {}

        self._build_styles()
        self._build_ui()
        self._prefill_hosts_from_config()
        self.root.after(100, self._process_events)

    def _build_styles(self) -> None:
        style = ttk.Style()
        style.configure("Pending.TLabel", foreground="#5c6770")
        style.configure("Running.TLabel", foreground="#005f99")
        style.configure("Success.TLabel", foreground="#20603d")
        style.configure("Error.TLabel", foreground="#9f2a2a")
        style.configure("Cancelled.TLabel", foreground="#a55d00")

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=12)
        root_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(1, weight=1)

        config_frame = ttk.LabelFrame(root_frame, text="Конфигурация", padding=12)
        config_frame.grid(row=0, column=0, sticky="ew")
        config_frame.columnconfigure(1, weight=1)

        ttk.Label(config_frame, text="Файл конфигурации").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(config_frame, textvariable=self.config_path_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(config_frame, text="Обзор", command=self._browse_config).grid(row=0, column=2, padx=(8, 0))

        outer = ttk.Notebook(root_frame)
        outer.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

        db_tab = ttk.Frame(outer)
        outer.add(db_tab, text="Восстановление БД")
        self._build_db_repair_tab(db_tab)

        tspiot_tab = ttk.Frame(outer)
        outer.add(tspiot_tab, text="Установка ТС ПИоТ")
        self.tspiot_panel = TsPiotPanel(self.root, tspiot_tab, self.config_path_var)

    def _build_db_repair_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        hosts_frame = ttk.LabelFrame(parent, text="Кассы", padding=12)
        hosts_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        hosts_frame.columnconfigure(0, weight=1)

        ttk.Label(
            hosts_frame,
            text="Адреса касс: по одному на строку, либо через запятую или точку с запятой.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.hosts_text = tk.Text(hosts_frame, height=4, wrap="word", font=("Consolas", 10))
        self.hosts_text.grid(row=1, column=0, sticky="ew")

        controls_frame = ttk.Frame(hosts_frame)
        controls_frame.grid(row=1, column=1, sticky="ns", padx=(8, 0))

        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Запустить все кассы", command=self._run_all_targets).grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=3,
            column=0,
            sticky="ew",
        )

        ttk.Label(parent, textvariable=self.overview_var).grid(row=1, column=0, sticky="w", padx=12, pady=(12, 8))

        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите config.toml",
            filetypes=(("TOML files", "*.toml"), ("All files", "*.*")),
        )
        if not path:
            return
        self.config_path_var.set(path)
        self._prefill_hosts_from_config()

    def _prefill_hosts_from_config(self) -> None:
        if self._hosts_text_value().strip():
            return
        try:
            config = load_config(self.config_path_var.get().strip())
        except ConfigError:
            return
        self.hosts_text.insert("1.0", config.connection.host)
        self._apply_hosts()

    def _hosts_text_value(self) -> str:
        return self.hosts_text.get("1.0", "end").strip()

    def _clear_hosts(self) -> None:
        if self._has_busy_targets():
            self.overview_var.set("Нельзя менять список касс во время выполнения.")
            return
        self.hosts_text.delete("1.0", "end")
        self._apply_hosts()

    def _apply_hosts(self) -> None:
        if self._has_busy_targets():
            self.overview_var.set("Нельзя менять список касс во время выполнения.")
            return

        hosts = _parse_hosts(self._hosts_text_value())
        current_hosts = set(self.targets)
        requested_hosts = set(hosts)

        for host in list(current_hosts - requested_hosts):
            self.notebook.forget(self.targets[host].frame)
            self.targets[host].destroy()
            del self.targets[host]

        for host in hosts:
            if host in self.targets:
                continue
            target = TargetView(self, self.notebook, host)
            self.targets[host] = target
            self.notebook.add(target.frame, text=host)

        if hosts and self.notebook.tabs():
            self.notebook.select(self.targets[hosts[0]].frame)

        self._refresh_overview()

    def _run_all_targets(self) -> None:
        if not self.targets and self._hosts_text_value().strip():
            self._apply_hosts()

        started = 0
        for host in _parse_hosts(self._hosts_text_value()):
            target = self.targets.get(host)
            if target is not None and target.run_all():
                started += 1

        if started == 0:
            if not self.targets:
                self.overview_var.set("Список касс пуст.")
            else:
                self._refresh_overview("Все кассы уже выполняют задачу.")
            return

        self._refresh_overview(f"Запущено касс: {started}.")

    def _cancel_all_targets(self) -> None:
        cancelled = 0
        for target in self.targets.values():
            if target.cancel():
                cancelled += 1
        if cancelled == 0:
            self._refresh_overview("Нет активных задач для отмены.")
            return
        self._refresh_overview(f"Запрошена отмена касс: {cancelled}.")

    def _process_events(self) -> None:
        while not self.event_queue.empty():
            event = self.event_queue.get()
            kind = event[0]

            if kind == "log":
                target = self.targets.get(event[1])
                if target is not None:
                    target.append_log(event[2])
            elif kind == "progress":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_progress(event[2], event[3], event[4])
            elif kind == "task-success":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_success(event[2], event[3])
            elif kind == "task-error":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_error(event[2], event[3])
            elif kind == "task-cancel":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_cancel(event[2], event[3])
            elif kind == "busy":
                target = self.targets.get(event[1])
                if target is not None:
                    target.set_busy(event[2])

            if kind in {"task-success", "task-error", "task-cancel", "busy"}:
                self._refresh_overview()

        self.root.after(100, self._process_events)

    def _refresh_overview(self, message: str | None = None) -> None:
        total = len(self.targets)
        running = sum(1 for target in self.targets.values() if target.busy)
        if message:
            self.overview_var.set(f"{message} Касс: {total}. Выполняется: {running}.")
            return
        if total == 0:
            self.overview_var.set("Список касс пуст.")
            return
        if running == 0:
            self.overview_var.set(f"Касс: {total}. Все готовы к запуску.")
            return
        self.overview_var.set(f"Касс: {total}. Выполняется: {running}.")

    def _has_busy_targets(self) -> bool:
        return any(target.busy for target in self.targets.values())


class TsPiotTargetView:
    TARGET_PRESETS: tuple[str, ...] = ("/usr/local/ukmclient", "/usr/local/lillo")

    def __init__(
        self,
        panel: "TsPiotPanel",
        notebook: ttk.Notebook,
        host: str,
        *,
        default_arch: str = "x64",
        default_target: str = "/usr/local/ukmclient",
    ):
        self.panel = panel
        self.host = host
        self.host_token = _host_token(host)
        self.frame = ttk.Frame(notebook, padding=12)
        self.summary_var = tk.StringVar(value="Готово к установке.")
        self.log_path_var = tk.StringVar()
        self.arch_var = tk.StringVar(value=default_arch)
        self.target_var = tk.StringVar(value=default_target)
        self.status_vars: dict[str, tk.StringVar] = {}
        self.detail_vars: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self.step_buttons: dict[str, ttk.Button] = {}
        self.busy = False
        self.cancel_event: threading.Event | None = None
        self.current_step_id: str | None = None

        queue_handler = QueueLogHandler(self.panel.event_queue, self.host)
        self.logger, log_path = configure_logger(
            logger_name=f"dbrepair.tspiot.{self.host_token}.{id(self)}",
            default_prefix=f"dbrepair-tspiot-{self.host_token}",
            include_stream=False,
            extra_handlers=[queue_handler],
        )
        self.log_path_var.set(str(log_path.resolve()))

        self._build_ui()
        self.reset_statuses()
        self.append_log(f"Лог сохраняется в {self.log_path_var.get()}")

    def _build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)

        info_frame = ttk.LabelFrame(self.frame, text="Подключение", padding=12)
        info_frame.grid(row=0, column=0, sticky="ew")
        info_frame.columnconfigure(1, weight=1)

        ttk.Label(info_frame, text="SSH/IP адрес").grid(row=0, column=0, sticky="w", padx=(0, 8))
        host_entry = ttk.Entry(info_frame)
        host_entry.insert(0, self.host)
        host_entry.configure(state="readonly")
        host_entry.grid(row=0, column=1, sticky="ew")

        ttk.Label(info_frame, text="Лог-файл").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Entry(info_frame, textvariable=self.log_path_var, state="readonly").grid(
            row=1,
            column=1,
            sticky="ew",
            pady=(10, 0),
        )

        controls_frame = ttk.Frame(self.frame, padding=(0, 12, 0, 12))
        controls_frame.grid(row=1, column=0, sticky="ew")
        controls_frame.columnconfigure(0, weight=1)

        options_frame = ttk.LabelFrame(controls_frame, text="Параметры установки", padding=12)
        options_frame.grid(row=0, column=0, sticky="ew")

        ttk.Label(options_frame, text="Архитектура:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Radiobutton(options_frame, text="x64", variable=self.arch_var, value="x64").grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Radiobutton(options_frame, text="x86", variable=self.arch_var, value="x86").grid(
            row=0, column=2, padx=(0, 16)
        )

        ttk.Label(options_frame, text="Каталог:").grid(row=0, column=3, sticky="w", padx=(0, 8))
        for index, preset in enumerate(self.TARGET_PRESETS):
            ttk.Radiobutton(options_frame, text=preset, variable=self.target_var, value=preset).grid(
                row=0, column=4 + index, padx=(0, 8)
            )

        self.detect_button = ttk.Button(options_frame, text="Определить по IP", command=self.detect)
        self.detect_button.grid(row=0, column=4 + len(self.TARGET_PRESETS), padx=(16, 0))

        actions_frame = ttk.Frame(controls_frame)
        actions_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions_frame.columnconfigure(4, weight=1)

        self.run_all_button = ttk.Button(actions_frame, text="Установить ТС ПИоТ", command=self.run_all)
        self.run_all_button.grid(row=0, column=0, padx=(0, 8))

        self.reset_button = ttk.Button(actions_frame, text="Сбросить статусы", command=self.reset_statuses)
        self.reset_button.grid(row=0, column=1, padx=(0, 8))

        self.cancel_button = ttk.Button(actions_frame, text="Отменить", command=self.cancel)
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))

        self.reboot_button = ttk.Button(actions_frame, text="Перезагрузить кассу", command=self.reboot)
        self.reboot_button.grid(row=0, column=3, padx=(0, 8))

        ttk.Label(actions_frame, textvariable=self.summary_var).grid(row=0, column=4, sticky="w")

        content = ttk.Frame(self.frame)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        steps_frame = ttk.Frame(content, padding=(12, 12, 12, 0))
        steps_frame.grid(row=0, column=0, sticky="ew")
        steps_frame.columnconfigure(1, weight=1)
        steps_frame.columnconfigure(3, weight=1)

        headers = ("Шаг", "Действие", "Статус", "Комментарий", "")
        for column, title in enumerate(headers):
            ttk.Label(steps_frame, text=title).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 8))

        for index, step in enumerate(TSPIOT_STEPS, start=1):
            ttk.Label(steps_frame, text=step.number).grid(row=index, column=0, sticky="nw", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, text=step.title).grid(row=index, column=1, sticky="ew", padx=(0, 8), pady=4)

            status_var = tk.StringVar()
            detail_var = tk.StringVar()
            status_label = ttk.Label(steps_frame, textvariable=status_var, style="Pending.TLabel")
            status_label.grid(row=index, column=2, sticky="w", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, textvariable=detail_var, wraplength=320).grid(
                row=index,
                column=3,
                sticky="ew",
                padx=(0, 8),
                pady=4,
            )
            button = ttk.Button(
                steps_frame,
                text="Выполнить",
                command=lambda step_id=step.step_id: self.run_step(step_id),
            )
            button.grid(row=index, column=4, sticky="e", pady=4)

            self.status_vars[step.step_id] = status_var
            self.detail_vars[step.step_id] = detail_var
            self.status_labels[step.step_id] = status_label
            self.step_buttons[step.step_id] = button

        log_frame = ttk.Frame(content, padding=(12, 12, 12, 12))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        ttk.Label(log_frame, text="Журнал выполнения").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log_text = tk.Text(log_frame, wrap="word", height=10, font=("Consolas", 10), state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def run_all(self) -> bool:
        return self.start_worker([step.step_id for step in TSPIOT_STEPS], reset=True)

    def run_step(self, step_id: str) -> bool:
        return self.start_worker([step_id], reset=False)

    def start_worker(self, step_ids: list[str], *, reset: bool) -> bool:
        if self.busy:
            return False
        if reset:
            self.reset_statuses()

        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.panel.set_tab_status(self.host, "running")
        self.summary_var.set("Установка...")
        thread = threading.Thread(
            target=self._worker_run_steps,
            args=(
                step_ids,
                self.panel.config_path_var.get().strip(),
                self.host,
                self.architecture(),
                self.target_base(),
                self.cancel_event,
            ),
            daemon=True,
        )
        thread.start()
        return True

    def architecture(self) -> str:
        return self.arch_var.get().strip() or "x64"

    def target_base(self) -> str:
        return self.target_var.get().strip() or self.TARGET_PRESETS[0]

    def detect(self) -> bool:
        if self.busy:
            return False
        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.summary_var.set("Определение параметров по IP...")
        thread = threading.Thread(
            target=self._worker_detect,
            args=(
                self.panel.config_path_var.get().strip(),
                self.host,
                self.cancel_event,
            ),
            daemon=True,
        )
        thread.start()
        return True

    def _worker_detect(self, config_path: str, host: str, cancel_event: threading.Event) -> None:
        try:
            config = load_config(config_path)
            config = override_host(config, host)
            self.logger.info("Detecting environment on %s", config.connection.host)
            architecture, target_base, raw = detect_environment(
                config,
                self.logger,
                cancel_event=cancel_event,
            )
            self.panel.event_queue.put(("detect", host, architecture, target_base, None))
        except OperationCancelledError:
            self.logger.info("Detection cancelled by user.")
            self.panel.event_queue.put(("detect", host, None, None, "Отменено пользователем."))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Environment detection failed.")
            self.panel.event_queue.put(("detect", host, None, None, str(exc)))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    def handle_detect(self, architecture: str | None, target_base: str | None, error: str | None) -> None:
        if error:
            self.summary_var.set(f"Не удалось определить: {_shorten(error)}")
            return
        parts: list[str] = []
        if architecture:
            self.arch_var.set(architecture)
            parts.append(f"архитектура {architecture}")
        if target_base:
            self.target_var.set(target_base)
            parts.append(f"каталог {target_base}")
        else:
            parts.append("каталог не найден (выбери вручную)")
        self.summary_var.set("Определено: " + ", ".join(parts) + ".")

    def reboot(self) -> bool:
        if self.busy:
            return False
        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.summary_var.set("Перезагрузка кассы...")
        thread = threading.Thread(
            target=self._worker_reboot,
            args=(
                self.panel.config_path_var.get().strip(),
                self.host,
                self.cancel_event,
            ),
            daemon=True,
        )
        thread.start()
        return True

    def _worker_reboot(self, config_path: str, host: str, cancel_event: threading.Event) -> None:
        try:
            config = load_config(config_path)
            config = override_host(config, host)
            self.logger.info("Rebooting host %s", config.connection.host)
            reboot_host(config, self.logger, cancel_event=cancel_event)
            self.panel.event_queue.put(("reboot", host, True, None))
        except OperationCancelledError:
            self.logger.info("Reboot cancelled by user.")
            self.panel.event_queue.put(("reboot", host, False, "Отменено пользователем."))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Reboot failed.")
            self.panel.event_queue.put(("reboot", host, False, str(exc)))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    def handle_reboot(self, ok: bool, error: str | None) -> None:
        if ok:
            self.summary_var.set("Команда перезагрузки отправлена.")
        else:
            self.summary_var.set(f"Перезагрузка не удалась: {_shorten(error)}")

    def _worker_run_steps(
        self,
        step_ids: list[str],
        config_path: str,
        host: str,
        architecture: str,
        target_base: str,
        cancel_event: threading.Event,
    ) -> None:
        progress_seen = False
        current_step_id: str | None = None

        def progress(step: TsPiotStep, status: str, details: str | None) -> None:
            nonlocal progress_seen, current_step_id
            progress_seen = True
            if status == "running":
                current_step_id = step.step_id
            elif status in {"success", "error"}:
                current_step_id = None
            self.panel.event_queue.put(("progress", host, step.step_id, status, details))

        try:
            config = load_config(config_path)
            if config.tspiot is None:
                raise ConfigError("В config.toml отсутствует секция [tspiot].")
            if config.webserver is None:
                raise ConfigError("В config.toml отсутствует секция [webserver] (base_url).")
            config = override_host(config, host)
            self.logger.info("Using config %s", config.source_path)
            self.logger.info("Target host %s", config.connection.host)

            # Автоопределение архитектуры и каталога по IP перед установкой,
            # чтобы не требовалось вручную жать "Определить по IP".
            try:
                det_arch, det_target, _ = detect_environment(config, self.logger, cancel_event=cancel_event)
                if det_arch:
                    architecture = det_arch
                if det_target:
                    target_base = det_target
                self.panel.event_queue.put(("detect", host, det_arch, det_target, None))
                self.logger.info("Автоопределение: arch=%s, каталог=%s", architecture, target_base)
            except OperationCancelledError:
                raise
            except (RuntimeError, OSError, TimeoutError) as exc:
                self.logger.warning(
                    "Не удалось определить окружение автоматически, использую выбранные значения (%s, %s): %s",
                    architecture,
                    target_base,
                    exc,
                )

            installer = TsPiotInstaller(
                config,
                self.logger,
                base_url=config.webserver.normalized(),
                architecture=architecture,
                target_base=target_base,
                cancel_event=cancel_event,
            )
            installer.run_steps(step_ids, progress=progress)
        except OperationCancelledError as exc:
            self.logger.info("TS PIoT installation cancelled by user.")
            self.panel.event_queue.put(("task-cancel", host, current_step_id, str(exc)))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("TS PIoT installation failed.")
            self.panel.event_queue.put(("task-error", host, step_ids if not progress_seen else [], str(exc)))
        else:
            self.panel.event_queue.put(("task-success", host, step_ids))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    def handle_progress(self, step_id: str, status: str, details: str | None) -> None:
        if status == "running":
            self.current_step_id = step_id
            self.set_step_state(step_id, "Выполняется", "Running.TLabel", "")
            return
        if status == "success":
            self.current_step_id = None
            self.set_step_state(step_id, "Успех", "Success.TLabel", details or "Выполнено")
            return
        if status == "error":
            self.current_step_id = None
            self.set_step_state(step_id, "Ошибка", "Error.TLabel", _shorten(details))
            self.summary_var.set("Установка остановлена с ошибкой. Подробности в журнале.")

    def handle_success(self, step_ids: list[str]) -> None:
        if len(step_ids) == len(TSPIOT_STEPS):
            self.summary_var.set("ТС ПИоТ установлен.")
        elif len(step_ids) == 1:
            self.summary_var.set("Шаг успешно выполнен.")
        else:
            self.summary_var.set("Выбранные шаги успешно выполнены.")
        self.current_step_id = None
        self.panel.set_tab_status(self.host, "success")

    def handle_error(self, step_ids: list[str], message: str) -> None:
        if step_ids:
            for step_id in step_ids:
                self.set_step_state(step_id, "Ошибка", "Error.TLabel", _shorten(message))
        self.summary_var.set(f"Ошибка: {_shorten(message)}")
        self.current_step_id = None
        self.panel.set_tab_status(self.host, "error")

    def handle_cancel(self, step_id: str | None, message: str) -> None:
        if step_id:
            self.set_step_state(step_id, "Отменено", "Cancelled.TLabel", _shorten(message))
        self.summary_var.set("Установка отменена пользователем.")
        self.current_step_id = None
        self.panel.set_tab_status(self.host, "cancel")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_all_button.configure(state=state)
        self.reset_button.configure(state=state)
        self.detect_button.configure(state=state)
        self.reboot_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        for button in self.step_buttons.values():
            button.configure(state=state)
        if not busy:
            self.cancel_event = None

    def reset_statuses(self) -> None:
        if self.busy:
            return
        for step in TSPIOT_STEPS:
            self.set_step_state(step.step_id, "Не запускался", "Pending.TLabel", "")
        self.summary_var.set("Готово к установке.")
        self.panel.set_tab_status(self.host, "idle")

    def set_step_state(self, step_id: str, text: str, style: str, detail: str) -> None:
        self.status_vars[step_id].set(text)
        self.detail_vars[step_id].set(detail)
        self.status_labels[step_id].configure(style=style)

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def cancel(self) -> bool:
        if not self.busy or self.cancel_event is None:
            return False
        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        self.summary_var.set("Отмена...")
        self.logger.info("Cancellation requested by user.")
        return True

    def destroy(self) -> None:
        self.cancel()
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
        self.frame.destroy()


class TsPiotPanel:
    TARGET_PRESETS: tuple[str, ...] = ("/usr/local/ukmclient", "/usr/local/lillo")

    def __init__(self, root: tk.Tk, parent: ttk.Frame, config_path_var: tk.StringVar):
        self.root = root
        self.config_path_var = config_path_var
        self.event_queue: queue.Queue[tuple] = queue.Queue()
        self.targets: dict[str, TsPiotTargetView] = {}
        self.overview_var = tk.StringVar(value="Добавьте адреса касс и создайте вкладки.")
        self.default_arch = "x64"
        self.default_target = self.TARGET_PRESETS[0]
        self._status_images = _make_status_images()

        self._build_ui(parent)
        self._prefill_from_config()
        self.root.after(100, self._process_events)

    def set_tab_status(self, host: str, status: str) -> None:
        target = self.targets.get(host)
        if target is None:
            return
        image = self._status_images.get(status)
        if image is None:
            return
        try:
            self.notebook.tab(target.frame, image=image, compound="left")
        except tk.TclError:
            pass

    def _build_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        hosts_frame = ttk.LabelFrame(parent, text="Кассы", padding=12)
        hosts_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        hosts_frame.columnconfigure(0, weight=1)

        ttk.Label(
            hosts_frame,
            text="Адреса касс: по одному на строку, либо через запятую или точку с запятой.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.hosts_text = tk.Text(hosts_frame, height=3, wrap="word", font=("Consolas", 10))
        self.hosts_text.grid(row=1, column=0, sticky="ew")

        controls_frame = ttk.Frame(hosts_frame)
        controls_frame.grid(row=1, column=1, sticky="ns", padx=(8, 0))

        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Установить на все", command=self._run_all_targets).grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Перезагрузить все", command=self._reboot_all_targets).grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=4,
            column=0,
            sticky="ew",
        )

        ttk.Label(parent, textvariable=self.overview_var).grid(row=1, column=0, sticky="w", padx=12, pady=(12, 8))

        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _prefill_from_config(self) -> None:
        if self._hosts_text_value().strip():
            return
        try:
            config = load_config(self.config_path_var.get().strip())
        except ConfigError:
            return
        self.hosts_text.insert("1.0", config.connection.host)
        if config.tspiot is not None:
            target = config.tspiot.target_base.rstrip("/")
            if target:
                self.default_target = target
        self._apply_hosts()

    def _hosts_text_value(self) -> str:
        return self.hosts_text.get("1.0", "end").strip()

    def _clear_hosts(self) -> None:
        if self._has_busy_targets():
            self.overview_var.set("Нельзя менять список касс во время выполнения.")
            return
        self.hosts_text.delete("1.0", "end")
        self._apply_hosts()

    def _apply_hosts(self) -> None:
        if self._has_busy_targets():
            self.overview_var.set("Нельзя менять список касс во время выполнения.")
            return

        hosts = _parse_hosts(self._hosts_text_value())
        current_hosts = set(self.targets)
        requested_hosts = set(hosts)

        for host in list(current_hosts - requested_hosts):
            self.notebook.forget(self.targets[host].frame)
            self.targets[host].destroy()
            del self.targets[host]

        for host in hosts:
            if host in self.targets:
                continue
            target = TsPiotTargetView(
                self,
                self.notebook,
                host,
                default_arch=self.default_arch,
                default_target=self.default_target,
            )
            self.targets[host] = target
            self.notebook.add(target.frame, text=host)
            self.set_tab_status(host, "idle")

        if hosts and self.notebook.tabs():
            self.notebook.select(self.targets[hosts[0]].frame)

        self._refresh_overview()

    def _run_all_targets(self) -> None:
        if not self.targets and self._hosts_text_value().strip():
            self._apply_hosts()

        started = 0
        for host in _parse_hosts(self._hosts_text_value()):
            target = self.targets.get(host)
            if target is not None and target.run_all():
                started += 1

        if started == 0:
            if not self.targets:
                self.overview_var.set("Список касс пуст.")
            else:
                self._refresh_overview("Все кассы уже выполняют задачу.")
            return

        self._refresh_overview(f"Запущено касс: {started}.")

    def _cancel_all_targets(self) -> None:
        cancelled = 0
        for target in self.targets.values():
            if target.cancel():
                cancelled += 1
        if cancelled == 0:
            self._refresh_overview("Нет активных задач для отмены.")
            return
        self._refresh_overview(f"Запрошена отмена касс: {cancelled}.")

    def _process_events(self) -> None:
        while not self.event_queue.empty():
            event = self.event_queue.get()
            kind = event[0]

            if kind == "log":
                target = self.targets.get(event[1])
                if target is not None:
                    target.append_log(event[2])
            elif kind == "progress":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_progress(event[2], event[3], event[4])
            elif kind == "task-success":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_success(event[2])
            elif kind == "task-error":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_error(event[2], event[3])
            elif kind == "task-cancel":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_cancel(event[2], event[3])
            elif kind == "detect":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_detect(event[2], event[3], event[4])
            elif kind == "reboot":
                target = self.targets.get(event[1])
                if target is not None:
                    target.handle_reboot(event[2], event[3])
            elif kind == "busy":
                target = self.targets.get(event[1])
                if target is not None:
                    target.set_busy(event[2])

            if kind in {"task-success", "task-error", "task-cancel", "busy"}:
                self._refresh_overview()

        self.root.after(100, self._process_events)

    def _refresh_overview(self, message: str | None = None) -> None:
        total = len(self.targets)
        running = sum(1 for target in self.targets.values() if target.busy)
        if message:
            self.overview_var.set(f"{message} Касс: {total}. Выполняется: {running}.")
            return
        if total == 0:
            self.overview_var.set("Список касс пуст.")
            return
        if running == 0:
            self.overview_var.set(f"Касс: {total}. Все готовы к установке.")
            return
        self.overview_var.set(f"Касс: {total}. Выполняется: {running}.")

    def _reboot_all_targets(self) -> None:
        if not self.targets and self._hosts_text_value().strip():
            self._apply_hosts()

        started = 0
        for host in _parse_hosts(self._hosts_text_value()):
            target = self.targets.get(host)
            if target is not None and target.reboot():
                started += 1

        if started == 0:
            if not self.targets:
                self.overview_var.set("Список касс пуст.")
            else:
                self._refresh_overview("Все кассы уже выполняют задачу.")
            return
        self._refresh_overview(f"Перезагрузка касс: {started}.")

    def _has_busy_targets(self) -> bool:
        return any(target.busy for target in self.targets.values())


def _make_status_images() -> dict[str, tk.PhotoImage]:
    colors = {
        "idle": "#c8c8c8",
        "running": "#33a0ff",
        "success": "#3ecf5a",
        "error": "#ff5b5b",
        "cancel": "#ffab33",
    }
    images: dict[str, tk.PhotoImage] = {}
    for key, color in colors.items():
        img = tk.PhotoImage(width=12, height=12)
        img.put(color, to=(0, 0, 12, 12))
        images[key] = img
    return images


def _parse_hosts(raw: str) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for candidate in re.split(r"[\s,;]+", raw.strip()):
        host = candidate.strip()
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


def expand_safe_step_chain(step_id: str) -> tuple[str, ...]:
    return SAFE_STEP_CHAINS.get(step_id, (step_id,))


def _step_number(step_id: str) -> str:
    for step in WORKFLOW_STEPS:
        if step.step_id == step_id:
            return step.number
    return step_id


def _host_token(host: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", host.strip())
    return token or "host"


def _shorten(message: str | None, limit: int = 96) -> str:
    if not message:
        return ""
    head = message.strip().splitlines()[0]
    if len(head) <= limit:
        return head
    return f"{head[: limit - 3]}..."


def launch_gui(config_path: str = "config.toml") -> None:
    root = tk.Tk()
    DbRepairGui(root, config_path=config_path)
    root.mainloop()


def main() -> None:
    launch_gui()


if __name__ == "__main__":
    main()
