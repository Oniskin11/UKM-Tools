from __future__ import annotations

import logging
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .config import ConfigError, load_config, override_host
from .distsource import build_source
from .logging_utils import configure_logger
from .remote import OperationCancelledError
from .tspiot import TSPIOT_STEPS, TsPiotInstaller, detect_environment, reboot_host
from .workflow import DbRepairWorkflow, WORKFLOW_STEPS, WorkflowArtifacts


SAFE_STEP_CHAINS: dict[str, tuple[str, ...]] = {
    "replace_datadir": ("replace_datadir", "restore_db", "start_ukmclient"),
    "restore_db": ("restore_db", "start_ukmclient"),
}


class QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: "queue.Queue[tuple]", host: str):
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


# --------------------------------------------------------------------------- #
# Базовые классы (общая механика для вкладок «Восстановление БД» и «ТС ПИоТ»)  #
# --------------------------------------------------------------------------- #


class BaseTargetView:
    """Вкладка одной кассы: таблица шагов, статусы, журнал, кнопки."""

    def __init__(
        self,
        panel: "BasePanel",
        notebook: ttk.Notebook,
        host: str,
        *,
        logger_name: str,
        log_prefix: str,
    ):
        self.panel = panel
        self.host = host
        self.host_token = _host_token(host)
        self.frame = ttk.Frame(notebook, padding=12)
        self.summary_var = tk.StringVar(value=self.ready_text())
        self.log_path_var = tk.StringVar()
        self.status_vars: dict[str, tk.StringVar] = {}
        self.detail_vars: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self.step_buttons: dict[str, ttk.Button] = {}
        self.busy = False
        self.cancel_event: threading.Event | None = None
        self.current_step_id: str | None = None
        self._changed = False

        self._init_extra_state()

        queue_handler = QueueLogHandler(self.panel.event_queue, self.host)
        self.logger, log_path = configure_logger(
            logger_name=f"{logger_name}.{self.host_token}.{id(self)}",
            default_prefix=f"{log_prefix}-{self.host_token}",
            include_stream=False,
            extra_handlers=[queue_handler],
        )
        self.log_path_var.set(str(log_path.resolve()))

        self._build_ui()
        self.reset_statuses()
        self.append_log(f"Лог сохраняется в {self.log_path_var.get()}")

    # --- Точки расширения для подклассов ---------------------------------- #

    def steps(self) -> tuple:
        raise NotImplementedError

    def run_all_text(self) -> str:
        return "Выполнить все шаги"

    def ready_text(self) -> str:
        return "Готово к запуску."

    def running_text(self) -> str:
        return "Выполнение..."

    def error_running_text(self) -> str:
        return "Выполнение остановлено с ошибкой. Подробности в журнале."

    def success_summary(self, step_ids: list[str]) -> str:
        if len(step_ids) == 1:
            return "Шаг успешно выполнен."
        return "Выбранные шаги успешно выполнены."

    def _init_extra_state(self) -> None:
        pass

    def _build_options(self, parent: ttk.Frame) -> None:
        pass

    def _extra_action_buttons(self, actions_frame: ttk.Frame, start_col: int) -> int:
        return start_col

    def _extra_busy_buttons(self) -> list[ttk.Button]:
        return []

    def _run(self, step_ids, config_path, host, cancel_event, progress) -> None:
        raise NotImplementedError

    # --- Построение интерфейса -------------------------------------------- #

    def _build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)

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
            row=1, column=1, sticky="ew", pady=(10, 0)
        )

        controls_frame = ttk.Frame(self.frame, padding=(0, 12, 0, 12))
        controls_frame.grid(row=1, column=0, sticky="ew")
        controls_frame.columnconfigure(0, weight=1)

        self._build_options(controls_frame)

        actions_frame = ttk.Frame(controls_frame)
        actions_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions_frame.columnconfigure(20, weight=1)

        self.run_all_button = ttk.Button(actions_frame, text=self.run_all_text(), command=self.run_all)
        self.run_all_button.grid(row=0, column=0, padx=(0, 8))
        self.reset_button = ttk.Button(actions_frame, text="Сбросить статусы", command=self.reset_statuses)
        self.reset_button.grid(row=0, column=1, padx=(0, 8))
        self.cancel_button = ttk.Button(actions_frame, text="Отменить", command=self.cancel)
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        next_col = self._extra_action_buttons(actions_frame, 3)
        ttk.Label(actions_frame, textvariable=self.summary_var).grid(row=0, column=20, sticky="w", padx=(8, 0))

        content = ttk.Frame(self.frame)
        content.grid(row=3, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        steps_frame = ttk.Frame(content, padding=(12, 12, 12, 0))
        steps_frame.grid(row=0, column=0, sticky="ew")
        steps_frame.columnconfigure(1, weight=1)
        steps_frame.columnconfigure(3, weight=1)

        headers = ("Шаг", "Действие", "Статус", "Комментарий", "")
        for column, title in enumerate(headers):
            ttk.Label(steps_frame, text=title).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 8))

        for index, step in enumerate(self.steps(), start=1):
            ttk.Label(steps_frame, text=step.number).grid(row=index, column=0, sticky="nw", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, text=step.title).grid(row=index, column=1, sticky="ew", padx=(0, 8), pady=4)

            status_var = tk.StringVar()
            detail_var = tk.StringVar()
            status_label = ttk.Label(steps_frame, textvariable=status_var, style="Pending.TLabel")
            status_label.grid(row=index, column=2, sticky="w", padx=(0, 8), pady=4)
            ttk.Label(steps_frame, textvariable=detail_var, wraplength=320).grid(
                row=index, column=3, sticky="ew", padx=(0, 8), pady=4
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

    # --- Запуск/остановка -------------------------------------------------- #

    def run_all(self) -> bool:
        return self.start_worker([step.step_id for step in self.steps()], reset=True)

    def run_step(self, step_id: str) -> bool:
        return self.start_worker([step_id], reset=False)

    def start_worker(self, step_ids: list[str], *, reset: bool) -> bool:
        if self.busy:
            return False
        if reset:
            self.reset_statuses()
        self.cancel_event = threading.Event()
        self.current_step_id = None
        self._changed = False
        self.set_busy(True)
        self.panel.set_tab_status(self.host, "running")
        self.summary_var.set(self.running_text())
        threading.Thread(
            target=self._worker,
            args=(list(step_ids), self.cancel_event),
            daemon=True,
        ).start()
        return True

    def _worker(self, step_ids: list[str], cancel_event: threading.Event) -> None:
        host = self.host
        config_path = self.panel.config_path_var.get().strip()
        progress_seen = False
        current_step_id: str | None = None

        def progress(step, status: str, details: str | None) -> None:
            nonlocal progress_seen, current_step_id
            progress_seen = True
            current_step_id = step.step_id if status == "running" else None
            self.panel.event_queue.put(("progress", host, step.step_id, status, details))

        try:
            self._run(step_ids, config_path, host, cancel_event, progress)
        except OperationCancelledError as exc:
            self.logger.info("Task cancelled by user.")
            self.panel.event_queue.put(("task-cancel", host, current_step_id, str(exc)))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Task failed.")
            self.panel.event_queue.put(("task-error", host, step_ids if not progress_seen else [], str(exc)))
        else:
            self.panel.event_queue.put(("task-success", host, step_ids))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    # --- Обработка событий (главный поток) -------------------------------- #

    def handle_progress(self, step_id: str, status: str, details: str | None) -> None:
        if status == "running":
            self.current_step_id = step_id
            self.set_step_state(step_id, "Выполняется", "Running.TLabel", "")
            return
        if status == "success":
            self.current_step_id = None
            self.set_step_state(step_id, "Успех", "Success.TLabel", details or "Выполнено")
            if not _is_skip_detail(details):
                self._changed = True
            return
        if status == "error":
            self.current_step_id = None
            self.set_step_state(step_id, "Ошибка", "Error.TLabel", _shorten(details))
            self.summary_var.set(self.error_running_text())

    def handle_success(self, step_ids: list[str]) -> None:
        self.current_step_id = None
        self.summary_var.set(self.success_summary(step_ids))
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
        self.summary_var.set("Выполнение отменено пользователем.")
        self.current_step_id = None
        self.panel.set_tab_status(self.host, "cancel")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_all_button.configure(state=state)
        self.reset_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        for button in self.step_buttons.values():
            button.configure(state=state)
        for button in self._extra_busy_buttons():
            button.configure(state=state)
        if not busy:
            self.cancel_event = None

    def reset_statuses(self) -> None:
        if self.busy:
            return
        for step in self.steps():
            self.set_step_state(step.step_id, "Не запускался", "Pending.TLabel", "")
        self.summary_var.set(self.ready_text())
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


class BasePanel:
    """Панель управления списком касс с вкладками (одна вкладка = одна касса)."""

    def __init__(self, root: tk.Tk, parent: ttk.Frame, config_path_var: tk.StringVar):
        self.root = root
        self.config_path_var = config_path_var
        self.event_queue: "queue.Queue[tuple]" = queue.Queue()
        self.targets: dict[str, BaseTargetView] = {}
        self.overview_var = tk.StringVar(value="Добавьте адреса касс и создайте вкладки.")
        self._status_images = _make_status_images()

        self._build_ui(parent)
        self._prefill_from_config()
        self.root.after(100, self._process_events)

    # --- Точки расширения -------------------------------------------------- #

    def create_target(self, host: str) -> BaseTargetView:
        raise NotImplementedError

    def _build_controls(self, controls_frame: ttk.Frame) -> None:
        raise NotImplementedError

    def _after_prefill(self, config) -> None:
        pass

    def _handle_event(self, kind: str, event: tuple, target: BaseTargetView | None) -> None:
        pass

    def ready_overview_text(self) -> str:
        return "Все готовы к запуску."

    # --- Индикатор статуса на вкладке ------------------------------------- #

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

    # --- Построение интерфейса -------------------------------------------- #

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

        self.hosts_text = tk.Text(hosts_frame, height=4, wrap="word", font=("Consolas", 10))
        self.hosts_text.grid(row=1, column=0, sticky="ew")

        controls_frame = ttk.Frame(hosts_frame)
        controls_frame.grid(row=1, column=1, sticky="ns", padx=(8, 0))
        self._build_controls(controls_frame)

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
        self._after_prefill(config)
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
            target = self.create_target(host)
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
        cancelled = sum(1 for target in self.targets.values() if target.cancel())
        if cancelled == 0:
            self._refresh_overview("Нет активных задач для отмены.")
            return
        self._refresh_overview(f"Запрошена отмена касс: {cancelled}.")

    def _process_events(self) -> None:
        while not self.event_queue.empty():
            event = self.event_queue.get()
            kind = event[0]
            target = self.targets.get(event[1]) if len(event) > 1 else None

            if kind == "log":
                if target is not None:
                    target.append_log(event[2])
            elif kind == "progress":
                if target is not None:
                    target.handle_progress(event[2], event[3], event[4])
            elif kind == "task-success":
                if target is not None:
                    target.handle_success(event[2])
            elif kind == "task-error":
                if target is not None:
                    target.handle_error(event[2], event[3])
            elif kind == "task-cancel":
                if target is not None:
                    target.handle_cancel(event[2], event[3])
            elif kind == "busy":
                if target is not None:
                    target.set_busy(event[2])
            else:
                self._handle_event(kind, event, target)

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
            self.overview_var.set(f"Касс: {total}. {self.ready_overview_text()}")
            return
        self.overview_var.set(f"Касс: {total}. Выполняется: {running}.")

    def _has_busy_targets(self) -> bool:
        return any(target.busy for target in self.targets.values())


# --------------------------------------------------------------------------- #
# Вкладка «Восстановление БД»                                                  #
# --------------------------------------------------------------------------- #


class DbRepairTargetView(BaseTargetView):
    def __init__(self, panel: "BasePanel", notebook: ttk.Notebook, host: str):
        self._artifacts: WorkflowArtifacts | None = None
        super().__init__(panel, notebook, host, logger_name="dbrepair.gui", log_prefix="dbrepair-gui")

    def steps(self) -> tuple:
        return WORKFLOW_STEPS

    def run_all_text(self) -> str:
        return "Выполнить все шаги"

    def run_step(self, step_id: str) -> bool:
        effective = list(expand_safe_step_chain(step_id))
        if len(effective) > 1:
            caption = " -> ".join(_step_number(candidate) for candidate in effective)
            self.summary_var.set(f"Для безопасности будет выполнена цепочка шагов {caption}.")
            self.logger.info("Step %s expanded to safe chain: %s", step_id, ", ".join(effective))
        return self.start_worker(effective, reset=False)

    def success_summary(self, step_ids: list[str]) -> str:
        if len(step_ids) == len(WORKFLOW_STEPS) and self._artifacts is not None:
            return (
                f"Готово. SQL копия: {self._artifacts.local_dump_copy.name}; "
                f"MySQL backup: {self._artifacts.remote_mysql_backup}"
            )
        if len(step_ids) == 1:
            return "Шаг успешно выполнен."
        return "Выбранные шаги успешно выполнены."

    def _run(self, step_ids, config_path, host, cancel_event, progress) -> None:
        config = load_config(config_path)
        config = override_host(config, host)
        self.logger.info("Using config %s", config.source_path)
        self.logger.info("Target host %s", config.connection.host)
        workflow = DbRepairWorkflow(config, self.logger, cancel_event=cancel_event)
        session = workflow.create_session()
        self._artifacts = workflow.run_steps(step_ids, session=session, progress=progress)


class DbRepairPanel(BasePanel):
    def create_target(self, host: str) -> BaseTargetView:
        return DbRepairTargetView(self, self.notebook, host)

    def ready_overview_text(self) -> str:
        return "Все готовы к запуску."

    def _build_controls(self, controls_frame: ttk.Frame) -> None:
        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Запустить все кассы", command=self._run_all_targets).grid(
            row=1, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=2, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=3, column=0, sticky="ew"
        )


# --------------------------------------------------------------------------- #
# Вкладка «Установка ТС ПИоТ»                                                  #
# --------------------------------------------------------------------------- #


class TsPiotTargetView(BaseTargetView):
    TARGET_PRESETS: tuple[str, ...] = ("/usr/local/ukmclient", "/usr/local/lillo")

    def __init__(
        self,
        panel: "BasePanel",
        notebook: ttk.Notebook,
        host: str,
        *,
        default_arch: str = "x64",
        default_target: str = "/usr/local/ukmclient",
    ):
        self._default_arch = default_arch
        self._default_target = default_target
        super().__init__(panel, notebook, host, logger_name="dbrepair.tspiot", log_prefix="dbrepair-tspiot")

    def _init_extra_state(self) -> None:
        self.arch_var = tk.StringVar(value=self._default_arch)
        self.target_var = tk.StringVar(value=self._default_target)

    def steps(self) -> tuple:
        return TSPIOT_STEPS

    def run_all_text(self) -> str:
        return "Установить ТС ПИоТ"

    def ready_text(self) -> str:
        return "Готово к установке."

    def running_text(self) -> str:
        return "Установка..."

    def error_running_text(self) -> str:
        return "Установка остановлена с ошибкой. Подробности в журнале."

    def architecture(self) -> str:
        return self.arch_var.get().strip() or "x64"

    def target_base(self) -> str:
        return self.target_var.get().strip() or self.TARGET_PRESETS[0]

    def success_summary(self, step_ids: list[str]) -> str:
        if len(step_ids) == len(TSPIOT_STEPS):
            if not self._changed:
                return "Изменений нет — всё уже актуально."
            return "ТС ПИоТ установлен."
        if len(step_ids) == 1:
            return "Шаг успешно выполнен."
        return "Выбранные шаги успешно выполнены."

    def _build_options(self, parent: ttk.Frame) -> None:
        options_frame = ttk.LabelFrame(parent, text="Параметры установки", padding=12)
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

    def _extra_action_buttons(self, actions_frame: ttk.Frame, start_col: int) -> int:
        self.reboot_button = ttk.Button(actions_frame, text="Перезагрузить кассу", command=self.reboot)
        self.reboot_button.grid(row=0, column=start_col, padx=(0, 8))
        return start_col + 1

    def _extra_busy_buttons(self) -> list[ttk.Button]:
        return [self.detect_button, self.reboot_button]

    def _run(self, step_ids, config_path, host, cancel_event, progress) -> None:
        config = load_config(config_path)
        if config.tspiot is None:
            raise ConfigError("В config.toml отсутствует секция [tspiot].")
        config = override_host(config, host)
        source = build_source(config)
        self.logger.info("Using config %s", config.source_path)
        self.logger.info("Target host %s", config.connection.host)

        def on_detect(arch: str, target: str | None) -> None:
            self.panel.event_queue.put(("autodetect", host, arch, target))

        installer = TsPiotInstaller(
            config,
            self.logger,
            source=source,
            architecture=self.architecture(),
            target_base=self.target_base(),
            cancel_event=cancel_event,
            auto_detect=True,
            on_detect=on_detect,
        )
        installer.run_steps(step_ids, progress=progress)

    # --- Определение окружения по IP (кнопка) ----------------------------- #

    def detect(self) -> bool:
        if self.busy:
            return False
        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.summary_var.set("Определение параметров по IP...")
        threading.Thread(
            target=self._worker_detect,
            args=(self.panel.config_path_var.get().strip(), self.host, self.cancel_event),
            daemon=True,
        ).start()
        return True

    def _worker_detect(self, config_path: str, host: str, cancel_event: threading.Event) -> None:
        try:
            config = load_config(config_path)
            config = override_host(config, host)
            self.logger.info("Detecting environment on %s", config.connection.host)
            architecture, target_base, _ = detect_environment(config, self.logger, cancel_event=cancel_event)
            self.panel.event_queue.put(("detect", host, architecture, target_base, None))
        except OperationCancelledError:
            self.panel.event_queue.put(("detect", host, None, None, "Отменено пользователем."))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Environment detection failed.")
            self.panel.event_queue.put(("detect", host, None, None, str(exc)))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    def apply_environment(self, architecture: str | None, target_base: str | None) -> None:
        """Тихо подставить архитектуру/каталог (используется авто-определением при установке)."""
        if architecture:
            self.arch_var.set(architecture)
        if target_base:
            self.target_var.set(target_base)

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

    # --- Перезагрузка кассы ------------------------------------------------ #

    def reboot(self) -> bool:
        if self.busy:
            return False
        self.cancel_event = threading.Event()
        self.current_step_id = None
        self.set_busy(True)
        self.panel.set_tab_status(self.host, "running")
        self.summary_var.set("Перезагрузка кассы...")
        threading.Thread(
            target=self._worker_reboot,
            args=(self.panel.config_path_var.get().strip(), self.host, self.cancel_event),
            daemon=True,
        ).start()
        return True

    def _worker_reboot(self, config_path: str, host: str, cancel_event: threading.Event) -> None:
        try:
            config = load_config(config_path)
            config = override_host(config, host)
            reboot_host(config, self.logger, cancel_event=cancel_event)
            self.panel.event_queue.put(("reboot", host, True, None))
        except OperationCancelledError:
            self.panel.event_queue.put(("reboot", host, False, "Отменено пользователем."))
        except (ConfigError, RuntimeError, OSError, TimeoutError, ValueError, KeyError) as exc:
            self.logger.exception("Reboot failed.")
            self.panel.event_queue.put(("reboot", host, False, str(exc)))
        finally:
            self.panel.event_queue.put(("busy", host, False))

    def handle_reboot(self, ok: bool, error: str | None) -> None:
        if ok:
            self.summary_var.set("Команда перезагрузки отправлена.")
            self.panel.set_tab_status(self.host, "idle")
        else:
            self.summary_var.set(f"Перезагрузка не удалась: {_shorten(error)}")
            self.panel.set_tab_status(self.host, "error")


class TsPiotPanel(BasePanel):
    def __init__(self, root: tk.Tk, parent: ttk.Frame, config_path_var: tk.StringVar):
        self.default_arch = "x64"
        self.default_target = TsPiotTargetView.TARGET_PRESETS[0]
        super().__init__(root, parent, config_path_var)

    def create_target(self, host: str) -> BaseTargetView:
        return TsPiotTargetView(
            self,
            self.notebook,
            host,
            default_arch=self.default_arch,
            default_target=self.default_target,
        )

    def ready_overview_text(self) -> str:
        return "Все готовы к установке."

    def _after_prefill(self, config) -> None:
        if config.tspiot is not None:
            target = config.tspiot.target_base.rstrip("/")
            if target:
                self.default_target = target

    def _build_controls(self, controls_frame: ttk.Frame) -> None:
        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Установить на все", command=self._run_all_targets).grid(
            row=1, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=2, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Перезагрузить все", command=self._reboot_all_targets).grid(
            row=3, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=4, column=0, sticky="ew"
        )

    def _reboot_all_targets(self) -> None:
        if not self.targets and self._hosts_text_value().strip():
            self._apply_hosts()
        started = 0
        for host in _parse_hosts(self._hosts_text_value()):
            target = self.targets.get(host)
            if isinstance(target, TsPiotTargetView) and target.reboot():
                started += 1
        if started == 0:
            if not self.targets:
                self.overview_var.set("Список касс пуст.")
            else:
                self._refresh_overview("Все кассы уже выполняют задачу.")
            return
        self._refresh_overview(f"Перезагрузка касс: {started}.")

    def _handle_event(self, kind: str, event: tuple, target: BaseTargetView | None) -> None:
        if not isinstance(target, TsPiotTargetView):
            return
        if kind == "detect":
            target.handle_detect(event[2], event[3], event[4])
        elif kind == "autodetect":
            target.apply_environment(event[2], event[3])
        elif kind == "reboot":
            target.handle_reboot(event[2], event[3])


# --------------------------------------------------------------------------- #
# Главное окно                                                                 #
# --------------------------------------------------------------------------- #


class DbRepairGui:
    def __init__(self, root: tk.Tk, config_path: str = "config.toml"):
        self.root = root
        self.root.title("DbRepair")
        self.root.minsize(1280, 760)

        self.config_path_var = tk.StringVar(value=config_path)

        self._build_styles()
        self._build_ui()

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
        self.db_panel = DbRepairPanel(self.root, db_tab, self.config_path_var)

        tspiot_tab = ttk.Frame(outer)
        outer.add(tspiot_tab, text="Установка ТС ПИоТ")
        self.tspiot_panel = TsPiotPanel(self.root, tspiot_tab, self.config_path_var)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите config.toml",
            filetypes=(("TOML files", "*.toml"), ("All files", "*.*")),
        )
        if path:
            self.config_path_var.set(path)


# --------------------------------------------------------------------------- #
# Вспомогательные функции                                                      #
# --------------------------------------------------------------------------- #


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


def _is_skip_detail(details: str | None) -> bool:
    if not details:
        return False
    low = details.lower()
    return low.startswith("уже") or low.startswith("каталог уже")


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
