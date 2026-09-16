from __future__ import annotations

import logging
import queue
import re
import threading
import tomllib
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import (
    ConfigError,
    load_config,
    override_host,
    save_publish_password,
    update_config_sections,
    write_default_config,
)
from .distsource import build_source
from .logging_utils import configure_logger
from .publisher import PublishError, detect_version, publish_distribution
from .remote import OperationCancelledError
from .kkt_time import KKT_TIME_STEPS, sync_kkt_time
from .mysql_rebuild import MYSQL_REBUILD_STEPS, MysqlRebuildWorkflow
from .tspiot import TSPIOT_STEPS, TsPiotInstaller, detect_environment, reboot_host
from .workflow import DbRepairWorkflow, WORKFLOW_STEPS, WorkflowArtifacts


SAFE_STEP_CHAINS: dict[str, tuple[str, ...]] = {
    "replace_datadir": tuple(step.step_id for step in WORKFLOW_STEPS),
    "restore_db": tuple(step.step_id for step in WORKFLOW_STEPS),
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
        self.frame.rowconfigure(2, weight=1)

        target_header = ttk.Frame(self.frame, padding=(4, 0, 4, 6))
        target_header.grid(row=0, column=0, sticky="ew")
        target_header.columnconfigure(1, weight=1)

        ttk.Label(target_header, text=f"Касса {self.host}", style="Target.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(target_header, text="Журнал", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(target_header, textvariable=self.log_path_var, state="readonly").grid(
            row=1, column=1, sticky="ew"
        )

        controls_frame = ttk.Frame(self.frame, padding=(0, 12, 0, 12))
        controls_frame.grid(row=1, column=0, sticky="ew")
        controls_frame.columnconfigure(0, weight=1)

        self._build_options(controls_frame)

        actions_frame = ttk.Frame(controls_frame)
        actions_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.run_all_button = ttk.Button(
            actions_frame, text=self.run_all_text(), command=self.run_all, style="Accent.TButton"
        )
        self.run_all_button.grid(row=0, column=0, padx=(0, 8))
        self.reset_button = ttk.Button(actions_frame, text="Сбросить статусы", command=self.reset_statuses)
        self.reset_button.grid(row=0, column=1, padx=(0, 8))
        self.cancel_button = ttk.Button(actions_frame, text="Отменить", command=self.cancel)
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        next_col = self._extra_action_buttons(actions_frame, 3)
        actions_frame.columnconfigure(next_col, weight=1)
        ttk.Label(actions_frame, textvariable=self.summary_var, style="Summary.TLabel").grid(
            row=0, column=next_col, sticky="w", padx=(8, 0)
        )

        content = ttk.PanedWindow(self.frame, orient="horizontal")
        content.grid(row=2, column=0, sticky="nsew")

        steps_frame = ttk.LabelFrame(content, text="План операции", padding=10)
        steps_frame.columnconfigure(1, weight=1)
        steps_frame.columnconfigure(3, weight=1)

        headers = ("Шаг", "Действие", "Статус", "Комментарий", "")
        for column, title in enumerate(headers):
            ttk.Label(steps_frame, text=title, style="StepHeader.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 8)
            )

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
                style="Step.TButton",
            )
            button.grid(row=index, column=4, sticky="e", pady=4)

            self.status_vars[step.step_id] = status_var
            self.detail_vars[step.step_id] = detail_var
            self.status_labels[step.step_id] = status_label
            self.step_buttons[step.step_id] = button

        log_frame = ttk.LabelFrame(content, text="Журнал выполнения", padding=10)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        ttk.Label(log_frame, text="Ход выполнения и сообщения об ошибках", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=10,
            font=("Cascadia Mono", 10),
            background="#152631",
            foreground="#dcecf1",
            insertbackground="#dcecf1",
            selectbackground="#2f7285",
            state="disabled",
            relief="flat",
            padx=8,
            pady=6,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        content.add(steps_frame, weight=3)
        content.add(log_frame, weight=2)

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

        hosts_frame = ttk.LabelFrame(parent, text="Кассы", padding=10)
        hosts_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        hosts_frame.columnconfigure(0, weight=1)

        ttk.Label(
            hosts_frame,
            text="Адреса касс: по одному на строку, либо через запятую или точку с запятой.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.hosts_text = tk.Text(hosts_frame, height=2, wrap="word", font=("Cascadia Mono", 10), padx=8, pady=6)
        self.hosts_text.grid(row=1, column=0, columnspan=2, sticky="ew")

        controls_frame = ttk.Frame(hosts_frame)
        controls_frame.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self._build_controls(controls_frame)

        ttk.Label(parent, textvariable=self.overview_var, style="Summary.TLabel").grid(
            row=1, column=0, sticky="w", padx=16, pady=(10, 8)
        )

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
        if step_id in {"replace_datadir", "restore_db"} and not messagebox.askyesno(
            "Подтверждение восстановления БД",
            f"На кассе {self.host} будет выполнено полное восстановление БД. "
            "Будет создан и проверен бэкап, затем старый datadir будет заменён. Продолжить?",
            parent=self.frame.winfo_toplevel(),
        ):
            return False
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
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Запустить все кассы", command=self._run_all_targets, style="Accent.TButton").grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=0, column=3
        )


# --------------------------------------------------------------------------- #
# Вкладка «Пересборка MySQL»                                                   #
# --------------------------------------------------------------------------- #


class MysqlRebuildTargetView(BaseTargetView):
    def __init__(self, panel: "BasePanel", notebook: ttk.Notebook, host: str):
        super().__init__(
            panel, notebook, host, logger_name="dbrepair.mysql-rebuild", log_prefix="dbrepair-mysql-rebuild"
        )

    def steps(self) -> tuple:
        return MYSQL_REBUILD_STEPS

    def run_all_text(self) -> str:
        return "Пересобрать MySQL"

    def ready_text(self) -> str:
        return "Готово к пересборке чистой MySQL. Данные текущей базы будут заменены."

    def running_text(self) -> str:
        return "Пересборка MySQL..."

    def run_step(self, step_id: str) -> bool:
        self.summary_var.set("Отдельные шаги отключены: пересборка выполняется только полным планом.")
        return False

    def run_all(self) -> bool:
        if not messagebox.askyesno(
            "Подтверждение пересборки MySQL",
            f"На кассе {self.host} текущий datadir MySQL будет заменён чистым. "
            "Прежний каталог будет сохранён как var_badN. Продолжить?",
            icon="warning",
            parent=self.frame.winfo_toplevel(),
        ):
            return False
        return self.start_worker([step.step_id for step in MYSQL_REBUILD_STEPS], reset=True)

    def success_summary(self, step_ids: list[str]) -> str:
        del step_ids
        return "MySQL пересобрана, схема UKM проверена, ukmclient запущен."

    def _run(self, step_ids, config_path, host, cancel_event, progress) -> None:
        config = override_host(load_config(config_path), host)
        self.logger.info("Using config %s", config.source_path)
        self.logger.info("Target host %s", config.connection.host)
        MysqlRebuildWorkflow(config, self.logger, cancel_event=cancel_event).run_steps(step_ids, progress=progress)


class MysqlRebuildPanel(BasePanel):
    def create_target(self, host: str) -> BaseTargetView:
        return MysqlRebuildTargetView(self, self.notebook, host)

    def ready_overview_text(self) -> str:
        return "Все готовы к пересборке MySQL."

    def _build_controls(self, controls_frame: ttk.Frame) -> None:
        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(
            controls_frame, text="Пересобрать на всех", command=self._run_all_targets, style="Accent.TButton"
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(row=0, column=3)


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
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Установить на все", command=self._run_all_targets, style="Accent.TButton").grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Перезагрузить все", command=self._reboot_all_targets).grid(
            row=0, column=3, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=0, column=4
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
# Вкладка «Синхронизация времени ККТ»                                         #
# --------------------------------------------------------------------------- #


class KktTimeTargetView(BaseTargetView):
    def __init__(self, panel: "BasePanel", notebook: ttk.Notebook, host: str):
        super().__init__(panel, notebook, host, logger_name="dbrepair.kkt-time", log_prefix="dbrepair-kkt-time")

    def steps(self) -> tuple:
        return KKT_TIME_STEPS

    def run_all_text(self) -> str:
        return "Синхронизировать время ККТ"

    def ready_text(self) -> str:
        return "Готово к синхронизации времени ККТ."

    def running_text(self) -> str:
        return "Синхронизация времени ККТ..."

    def success_summary(self, step_ids: list[str]) -> str:
        return "Время ККТ синхронизировано."

    def _run(self, step_ids, config_path, host, cancel_event, progress) -> None:
        if list(step_ids) != [KKT_TIME_STEPS[0].step_id]:
            raise ValueError("Поддерживается только синхронизация времени ККТ.")
        config = override_host(load_config(config_path), host)
        self.logger.info("Using config %s", config.source_path)
        self.logger.info("Target host %s", config.connection.host)
        sync_kkt_time(config, self.logger, cancel_event=cancel_event, progress=progress)


class KktTimePanel(BasePanel):
    def create_target(self, host: str) -> BaseTargetView:
        return KktTimeTargetView(self, self.notebook, host)

    def ready_overview_text(self) -> str:
        return "Все готовы к синхронизации времени ККТ."

    def _build_controls(self, controls_frame: ttk.Frame) -> None:
        ttk.Button(controls_frame, text="Обновить список", command=self._apply_hosts).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(
            controls_frame, text="Синхронизировать все", command=self._run_all_targets, style="Accent.TButton"
        ).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Отменить все", command=self._cancel_all_targets).grid(
            row=0, column=2, padx=(0, 8)
        )
        ttk.Button(controls_frame, text="Очистить список", command=self._clear_hosts).grid(
            row=0, column=3
        )


# --------------------------------------------------------------------------- #
# Вкладка «Публикация драйвера»                                                #
# --------------------------------------------------------------------------- #


class PublishPanel:
    """Публикация ККТ и ТС ПИоТ из одного ZIP или каталога на веб-сервер."""

    def __init__(self, root: tk.Tk, parent: ttk.Frame, config_path_var: tk.StringVar):
        self.root = root
        self.config_path_var = config_path_var
        self.event_queue: "queue.Queue[tuple]" = queue.Queue()
        self.source_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="Выберите zip-архив или папку с драйвером ККТ и/или ТС ПИоТ.")
        self.busy = False

        queue_handler = QueueLogHandler(self.event_queue, "publish")
        self.logger, _ = configure_logger(
            logger_name=f"dbrepair.publish.{id(self)}",
            default_prefix="dbrepair-publish",
            include_stream=False,
            extra_handlers=[queue_handler],
        )

        self._build_ui(parent)
        self.root.after(100, self._process_events)

    def _build_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        content = ttk.PanedWindow(parent, orient="vertical")
        content.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        form = ttk.LabelFrame(content, text="Драйвер ККТ", padding=12)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Файл (.zip) или папка дистрибутива").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(form, textvariable=self.source_var).grid(row=0, column=1, sticky="ew")
        btns = ttk.Frame(form)
        btns.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(btns, text="Обзор файла", command=self._browse_file).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(btns, text="Обзор папки", command=self._browse_dir).grid(row=0, column=1)

        ttk.Label(form, text="Версия").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Entry(form, textvariable=self.version_var).grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Label(form, text="(определяется из имени, можно поправить)").grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(10, 0)
        )

        actions = ttk.Frame(form)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        actions.columnconfigure(1, weight=1)
        self.publish_button = ttk.Button(
            actions, text="Опубликовать на веб-сервер", command=self.publish, style="Accent.TButton"
        )
        self.publish_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Label(actions, textvariable=self.summary_var, style="Summary.TLabel").grid(row=0, column=1, sticky="w")

        log_frame = ttk.LabelFrame(content, text="Журнал публикации", padding=10)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Перетащите разделитель выше или ниже, чтобы изменить размер журнала.", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=14,
            font=("Cascadia Mono", 10),
            background="#152631",
            foreground="#dcecf1",
            insertbackground="#dcecf1",
            selectbackground="#2f7285",
            state="disabled",
            relief="flat",
            padx=8,
            pady=6,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        content.add(form, weight=1)
        content.add(log_frame, weight=3)

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите архив драйвера",
            filetypes=(("ZIP archives", "*.zip"), ("All files", "*.*")),
        )
        if path:
            self.source_var.set(path)
            self._autofill_version(path)

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку драйвера")
        if path:
            self.source_var.set(path)
            self._autofill_version(path)

    def _autofill_version(self, path: str) -> None:
        version = detect_version(Path(path).name)
        if version:
            self.version_var.set(version)

    def publish(self) -> None:
        if self.busy:
            return
        source = self.source_var.get().strip()
        if not source:
            self.summary_var.set("Укажите файл или папку драйвера.")
            return

        config_path = self.config_path_var.get().strip()
        try:
            config = load_config(config_path)
        except ConfigError as exc:
            self.summary_var.set(f"Ошибка конфига: {_shorten(str(exc))}")
            return
        if config.publish is None:
            self.summary_var.set("В config.toml нет секции [publish].")
            return

        answer = self._ask_password(config.publish.host, config.publish.username, config.publish.password)
        if answer is None:
            self.summary_var.set("Публикация отменена.")
            return
        password, save = answer
        if not password:
            self.summary_var.set("Пароль не введён.")
            return

        self.busy = True
        self.publish_button.configure(state="disabled")
        self.summary_var.set("Публикация...")
        threading.Thread(
            target=self._worker,
            args=(config_path, source, self.version_var.get().strip(), password, save),
            daemon=True,
        ).start()

    def _ask_password(self, host: str, username: str, current: str | None):
        dialog = tk.Toplevel(self.root)
        dialog.title("Пароль для публикации")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=f"Веб-сервер: {username}@{host}").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Пароль SSH:").grid(row=1, column=0, sticky="w", pady=(10, 0), padx=(0, 8))
        pwd_var = tk.StringVar(value=current or "")
        save_var = tk.BooleanVar(value=bool(current))
        entry = ttk.Entry(frame, textvariable=pwd_var, show="*", width=28)
        entry.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(frame, text="Сохранить пароль в config.toml", variable=save_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))

        result: dict[str, object] = {}

        def ok() -> None:
            result["password"] = pwd_var.get()
            result["save"] = save_var.get()
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="OK", command=ok).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=cancel).grid(row=0, column=1)
        entry.focus_set()
        dialog.bind("<Return>", lambda _event: ok())
        dialog.bind("<Escape>", lambda _event: cancel())
        self.root.wait_window(dialog)

        if "password" not in result:
            return None
        return str(result["password"]), bool(result["save"])

    def _worker(self, config_path: str, source: str, version: str, password: str, save: bool) -> None:
        try:
            config = load_config(config_path)
            if config.publish is None:
                raise PublishError("В config.toml отсутствует секция [publish] (доступ к веб-серверу).")
            publish_config = replace(config.publish, password=password)
            result = publish_distribution(publish_config, Path(source), self.logger, version=version or None)
            if save:
                try:
                    save_publish_password(config_path, password)
                    self.logger.info("Пароль сохранён в %s", config_path)
                except OSError as exc:
                    self.logger.warning("Не удалось сохранить пароль в конфиг: %s", exc)
            self.event_queue.put(("publish-result", result, None))
        except (PublishError, ConfigError, RuntimeError, OSError, ValueError) as exc:
            self.logger.exception("Publish failed.")
            self.event_queue.put(("publish-result", None, str(exc)))
        finally:
            self.event_queue.put(("publish-busy", False))

    def _process_events(self) -> None:
        while not self.event_queue.empty():
            event = self.event_queue.get()
            kind = event[0]
            if kind == "log":
                self._append_log(event[2])
            elif kind == "publish-busy":
                self.busy = event[1]
                self.publish_button.configure(state="disabled" if self.busy else "normal")
            elif kind == "publish-result":
                result, error = event[1], event[2]
                if error:
                    self.summary_var.set(f"Ошибка: {_shorten(error)}")
                else:
                    parts = []
                    if result.hashes:
                        parts.append("KKT: " + ", ".join(sorted(result.hashes)))
                    if result.tspiot_hashes:
                        parts.append("ТС ПИоТ: " + ", ".join(sorted(result.tspiot_hashes)))
                    self.summary_var.set(f"Опубликовано v{result.version} ({'; '.join(parts)}).")
        self.root.after(100, self._process_events)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


# --------------------------------------------------------------------------- #
# Диалог настроек                                                              #
# --------------------------------------------------------------------------- #


class SettingsDialog:
    """Полные настройки config.toml по секциям (вкладки)."""

    # (ключ, подпись, тип, значение по умолчанию)
    SECTION_SPECS: tuple = (
        ("connection", "Подключение", (
            ("host", "Хост кассы", "str", ""),
            ("port", "Порт", "int", 22),
            ("username", "Пользователь", "str", "root"),
            ("password", "Пароль", "password", ""),
            ("sudo_password", "Пароль sudo", "password", ""),
            ("use_sudo", "Использовать sudo", "bool", False),
            ("timeout", "Таймаут, сек", "float", 20.0),
            ("key_filename", "Ключ (файл)", "path_file", ""),
        )),
        ("database", "База данных", (
            ("name", "Имя БД", "str", "ukmclient"),
            ("password", "Пароль БД", "password", ""),
        )),
        ("paths", "Пути (восстановление БД)", (
            ("dbrepair_archive", "Архив dbrepair (.tgz)", "path_file", ""),
            ("empty_datadir_archive", "Архив пустого datadir (.tgz)", "path_file", ""),
            ("local_backup_dir", "Локальный каталог бэкапов", "path_dir", "backups"),
            ("remote_tmp_dir", "Временный каталог на кассе", "str", "/tmp"),
            ("remote_dbrepair_dir_name", "Имя каталога dbrepair", "str", ""),
            ("remote_mysql_dir", "Каталог MySQL", "str", "/usr/local/mysql"),
            ("remote_mysql_var_dir", "Каталог данных MySQL", "str", "/usr/local/mysql/var"),
            ("remote_mysql_backup_name", "Имя бэкапа datadir", "str", "mysql-db.tgz"),
            ("remote_my_cnf", "Путь к my.cnf", "str", "/etc/my.cnf"),
            ("remote_dump_filename", "Имя файла дампа", "str", "ukmclient.sql"),
        )),
        ("services", "Сервисы", (
            ("mysql_stop", "Стоп MySQL", "str", "/etc/init.d/mysql stop"),
            ("mysql_start", "Старт MySQL", "str", "/etc/init.d/mysql start"),
            ("ukmclient_stop", "Стоп ukmclient", "str", "/etc/init.d/ukmclient stop"),
            ("ukmclient_start", "Старт ukmclient", "str", "/etc/init.d/ukmclient start"),
        )),
        ("tspiot", "ТС ПИоТ", (
            ("target_base", "Каталог установки", "str", "/usr/local/ukmclient"),
            ("data_dir_name", "Имя каталога данных", "str", "data_tspiot"),
            ("binary_name", "Имя бинарника", "str", "tspiot"),
            ("gismt_cert_name", "Имя сертификата", "str", "gismt_cert.txt"),
            ("owner", "Владелец (пусто = из каталога)", "str", ""),
        )),
        ("publish", "Публикация", (
            ("host", "Хост веб-сервера", "str", ""),
            ("port", "Порт", "int", 22),
            ("username", "Пользователь", "str", "root"),
            ("password", "Пароль", "password", ""),
            ("ukm_dir", "Каталог UKM", "str", "/var/www/files/UKM"),
            ("owner", "Владелец файлов", "str", "www-data:www-data"),
        )),
    )

    def __init__(self, root: tk.Tk, config_path: str, on_saved=None):
        self.root = root
        self.config_path = config_path
        self.on_saved = on_saved
        self.vars: dict[tuple[str, str], tuple] = {}

        self.win = tk.Toplevel(root)
        self.win.title("Настройки")
        self.win.transient(root)
        self.win.resizable(False, False)
        self.win.grab_set()

        self.source_var = tk.StringVar(value="http")
        self.base_url_var = tk.StringVar(value="http://192.168.20.229/UKM/")
        self.local_dir_var = tk.StringVar()
        self.status_var = tk.StringVar()

        self._raw = self._read_raw()
        self._build()
        self._on_source_change()
        self.root.wait_window(self.win)

    def _read_raw(self) -> dict:
        try:
            return tomllib.loads(Path(self.config_path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def _build(self) -> None:
        frame = ttk.Frame(self.win, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        for section, title, spec in self.SECTION_SPECS:
            tab = ttk.Frame(notebook, padding=12)
            tab.columnconfigure(1, weight=1)
            notebook.add(tab, text=title)
            section_raw = self._raw.get(section, {}) if isinstance(self._raw.get(section), dict) else {}
            for row, (key, label, kind, default) in enumerate(spec):
                value = section_raw.get(key, default)
                self._add_field(tab, row, section, key, label, kind, value)

        self._build_source_tab(notebook)

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Сохранить", command=self._save).grid(row=0, column=1, padx=(8, 8))
        ttk.Button(bottom, text="Отмена", command=self.win.destroy).grid(row=0, column=2)

    def _build_source_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=12)
        tab.columnconfigure(0, weight=1)
        notebook.add(tab, text="Источник")

        dist = self._raw.get("distribution", {}) if isinstance(self._raw.get("distribution"), dict) else {}
        webserver = self._raw.get("webserver", {}) if isinstance(self._raw.get("webserver"), dict) else {}
        local_dir = str(dist.get("local_dir", "") or "")
        base_url = str(dist.get("base_url") or webserver.get("base_url") or "http://192.168.20.229/UKM/")
        self.local_dir_var.set(local_dir)
        self.base_url_var.set(base_url)
        self.source_var.set("local" if local_dir.strip() else "http")

        ttk.Radiobutton(
            tab, text="HTTP-сервер (касса качает сама)", value="http",
            variable=self.source_var, command=self._on_source_change,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            tab, text="Локальный каталог (заливка по SFTP)", value="local",
            variable=self.source_var, command=self._on_source_change,
        ).grid(row=1, column=0, sticky="w")

        self.http_box = ttk.LabelFrame(tab, text="HTTP-сервер", padding=10)
        self.http_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.http_box.columnconfigure(1, weight=1)
        ttk.Label(self.http_box, text="Базовый URL").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(self.http_box, textvariable=self.base_url_var, width=44).grid(row=0, column=1, sticky="ew")

        self.local_box = ttk.LabelFrame(tab, text="Локальный каталог", padding=10)
        self.local_box.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.local_box.columnconfigure(1, weight=1)
        ttk.Label(self.local_box, text="Каталог").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(self.local_box, textvariable=self.local_dir_var, width=36).grid(row=0, column=1, sticky="ew")
        ttk.Button(self.local_box, text="Обзор", command=self._browse_local).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(
            tab, text="Публикация драйверов доступна только при HTTP-источнике (вкладка «Публикация»)."
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

    def _add_field(self, parent, row, section, key, label, kind, value) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(parent, variable=var).grid(row=row, column=1, sticky="w", pady=2)
        else:
            var = tk.StringVar(value="" if value is None else str(value))
            show = "*" if kind == "password" else ""
            ttk.Entry(parent, textvariable=var, show=show, width=42).grid(row=row, column=1, sticky="ew", pady=2)
            if kind in ("path_file", "path_dir"):
                ttk.Button(
                    parent, text="Обзор", command=lambda v=var, k=kind: self._browse(v, k)
                ).grid(row=row, column=2, padx=(6, 0))
        self.vars[(section, key)] = (var, kind)

    def _browse(self, var: tk.StringVar, kind: str) -> None:
        path = filedialog.askdirectory() if kind == "path_dir" else filedialog.askopenfilename()
        if path:
            var.set(path)

    def _browse_local(self) -> None:
        path = filedialog.askdirectory(title="Каталог с дистрибутивами (UKM)")
        if path:
            self.local_dir_var.set(path)

    def _set_state(self, box: ttk.LabelFrame, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for child in box.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass

    def _on_source_change(self) -> None:
        is_http = self.source_var.get() == "http"
        self._set_state(self.http_box, is_http)
        self._set_state(self.local_box, not is_http)

    def _save(self) -> None:
        updates: dict[str, dict[str, object]] = {}
        for section, _title, spec in self.SECTION_SPECS:
            section_updates: dict[str, object] = {}
            for key, _label, kind, default in spec:
                var, _kind = self.vars[(section, key)]
                if kind == "bool":
                    section_updates[key] = bool(var.get())
                elif kind == "int":
                    section_updates[key] = _to_number(var.get(), int, default)
                elif kind == "float":
                    section_updates[key] = _to_number(var.get(), float, default)
                else:
                    text = var.get().strip()
                    if text:
                        section_updates[key] = text
            if section_updates:
                updates[section] = section_updates

        source = self.source_var.get()
        distribution: dict[str, object] = {
            "local_dir": self.local_dir_var.get().strip() if source == "local" else "",
        }
        base_url = self.base_url_var.get().strip()
        if base_url:
            distribution["base_url"] = base_url
        updates["distribution"] = distribution

        try:
            update_config_sections(self.config_path, updates)
        except OSError as exc:
            self.status_var.set(f"Не удалось сохранить: {exc}")
            return

        if self.on_saved is not None:
            self.on_saved()
        self.win.destroy()


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
        self._ensure_config_exists()
        self._apply_source_visibility()

    def _build_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f4f7fa", foreground="#22313f")
        style.configure("TFrame", background="#f4f7fa")
        style.configure("TLabel", background="#f4f7fa")
        style.configure("TLabelframe", background="#f4f7fa", bordercolor="#cbd5df", relief="solid")
        style.configure("TLabelframe.Label", background="#f4f7fa", foreground="#35536b", font=("Segoe UI Semibold", 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("Step.TButton", padding=(8, 3))
        style.configure("Accent.TButton", background="#176b87", foreground="#ffffff", padding=(12, 6))
        style.map(
            "Accent.TButton",
            background=[("disabled", "#9baab5"), ("active", "#0e5871")],
            foreground=[("disabled", "#edf2f5")],
        )
        style.configure("Field.TLabel", foreground="#597083")
        style.configure("Target.TLabel", foreground="#183f55", font=("Segoe UI Semibold", 13))
        style.configure("StepHeader.TLabel", foreground="#35536b", font=("Segoe UI Semibold", 9))
        style.configure("Hint.TLabel", foreground="#647b8c", font=("Segoe UI", 9))
        style.configure("Summary.TLabel", foreground="#175b73", font=("Segoe UI Semibold", 10))
        style.configure("Pending.TLabel", foreground="#667583")
        style.configure("Running.TLabel", foreground="#126e8a", font=("Segoe UI Semibold", 10))
        style.configure("Success.TLabel", foreground="#24734d", font=("Segoe UI Semibold", 10))
        style.configure("Error.TLabel", foreground="#ae3838", font=("Segoe UI Semibold", 10))
        style.configure("Cancelled.TLabel", foreground="#a35c10", font=("Segoe UI Semibold", 10))

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=12)
        root_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(1, weight=1)

        config_frame = ttk.LabelFrame(root_frame, text="Рабочая конфигурация", padding=12)
        config_frame.grid(row=0, column=0, sticky="ew")
        config_frame.columnconfigure(1, weight=1)

        ttk.Label(config_frame, text="Файл конфигурации").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(config_frame, textvariable=self.config_path_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(config_frame, text="Обзор", command=self._browse_config).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(config_frame, text="Настройки…", command=self._open_settings).grid(row=0, column=3, padx=(8, 0))

        self.outer = ttk.Notebook(root_frame)
        self.outer.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

        db_tab = ttk.Frame(self.outer)
        self.outer.add(db_tab, text="Восстановление БД")
        self.db_panel = DbRepairPanel(self.root, db_tab, self.config_path_var)

        mysql_rebuild_tab = ttk.Frame(self.outer)
        self.outer.add(mysql_rebuild_tab, text="Пересборка MySQL")
        self.mysql_rebuild_panel = MysqlRebuildPanel(self.root, mysql_rebuild_tab, self.config_path_var)

        tspiot_tab = ttk.Frame(self.outer)
        self.outer.add(tspiot_tab, text="Установка ТС ПИоТ")
        self.tspiot_panel = TsPiotPanel(self.root, tspiot_tab, self.config_path_var)

        kkt_time_tab = ttk.Frame(self.outer)
        self.outer.add(kkt_time_tab, text="Синхронизация времени ККТ")
        self.kkt_time_panel = KktTimePanel(self.root, kkt_time_tab, self.config_path_var)

        self.publish_tab = ttk.Frame(self.outer)
        self.outer.add(self.publish_tab, text="Публикация файлов")
        self.publish_panel = PublishPanel(self.root, self.publish_tab, self.config_path_var)

    def _ensure_config_exists(self) -> None:
        """Создать стартовый config.toml, если GUI запустили впервые."""
        configured_path = self.config_path_var.get().strip() or "config.toml"
        path = Path(configured_path)
        if not path.is_file():
            write_default_config(path)
        self.config_path_var.set(str(path))

    def _current_source_is_http(self) -> bool:
        try:
            config = load_config(self.config_path_var.get().strip())
        except ConfigError:
            return True
        dist = config.distribution
        if dist is not None and dist.local_dir is not None:
            return False
        if dist is not None and dist.base_url:
            return True
        return config.webserver is not None or dist is None

    def _apply_source_visibility(self) -> None:
        """Вкладка «Публикация драйвера» видна только при HTTP-источнике."""
        is_http = self._current_source_is_http()
        try:
            if is_http:
                self.outer.add(self.publish_tab, text="Публикация файлов")
            else:
                self.outer.hide(self.publish_tab)
        except tk.TclError:
            pass

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите config.toml",
            filetypes=(("TOML files", "*.toml"), ("All files", "*.*")),
        )
        if path:
            self.config_path_var.set(path)
            self._apply_source_visibility()

    def _open_settings(self) -> None:
        SettingsDialog(self.root, self.config_path_var.get().strip(), on_saved=self._apply_source_visibility)


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


def _to_number(text: object, caster, default):
    try:
        return caster(str(text).strip())
    except (ValueError, TypeError):
        return default


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
