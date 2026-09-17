"""Русские сообщения для журнала и рабочего интерфейса.

Команды, пути и ответ оборудования намеренно не переводятся: это технические
данные, по которым оператор и разработчик могут воспроизвести ошибку.
"""

from __future__ import annotations

import re


_EXACT_MESSAGES = {
    "Task cancelled by user.": "Задача отменена пользователем.",
    "Cancellation requested by user.": "Пользователь запросил отмену операции.",
    "Task failed.": "Задача завершилась с ошибкой.",
    "Workflow failed.": "Восстановление БД завершилось с ошибкой.",
    "Workflow completed successfully.": "Восстановление БД успешно завершено.",
    "Environment detection failed.": "Не удалось определить окружение кассы.",
    "Reboot failed.": "Не удалось перезагрузить кассу.",
    "Publish failed.": "Не удалось опубликовать файлы.",
    "Reboot command sent.": "Команда перезагрузки отправлена.",
    "MySQL is ready.": "MySQL готов к работе.",
    "MySQL is stopped.": "MySQL остановлен.",
    "ukmclient is running.": "ukmclient запущен.",
    "ukmclient is stopped.": "ukmclient остановлен.",
    "TS PIoT installation completed successfully.": "Установка ТС ПИоТ успешно завершена.",
}


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^Using config (.+)$"), r"Используется конфигурация \1"),
    (re.compile(r"^Target host (.+)$"), r"Целевая касса: \1"),
    (re.compile(r"^SSH connection established to (.+)$"), r"Установлено SSH-подключение к \1"),
    (re.compile(r"^RUN (.+)$", re.DOTALL), r"Выполнение команды: \1"),
    (re.compile(r"^STDOUT (.+)$", re.DOTALL), r"Стандартный вывод: \1"),
    (re.compile(r"^STDERR (.+)$", re.DOTALL), r"Стандартный вывод ошибок: \1"),
    (re.compile(r"^UPLOAD (.+)$"), r"Загрузка на кассу: \1"),
    (re.compile(r"^DOWNLOAD (.+)$"), r"Скачивание с кассы: \1"),
    (re.compile(r"^Step (.+)$"), r"Шаг \1"),
    (re.compile(r"^Starting workflow for (.+)$"), r"Запуск восстановления БД для \1"),
    (re.compile(r"^Starting TS PIoT installation on (.+)$"), r"Запуск установки ТС ПИоТ на \1"),
    (re.compile(r"^Starting KKT time synchronization on (.+) via RS-232 API$"), r"Запуск синхронизации времени ККТ на \1 через RS-232 API"),
    (re.compile(r"^KKT time synchronization completed on (.+) via RS-232 API$"), r"Синхронизация времени ККТ на \1 через RS-232 API завершена"),
    (re.compile(r"^Detecting environment on (.+)$"), r"Определение окружения на \1"),
    (re.compile(r"^Detected machine '(.+)' -> architecture (.+)$"), r"Определена система «\1», архитектура \2"),
    (re.compile(r"^Detected target directory (.+)$"), r"Определён каталог установки \1"),
    (re.compile(r"^No known target directory found among: (.+)$"), r"Не найден известный каталог установки среди: \1"),
    (re.compile(r"^Sending reboot command to (.+)$"), r"Отправка команды перезагрузки на \1"),
    (re.compile(r"^Source: (.+)$"), r"Источник: \1"),
    (re.compile(r"^Target: (.+)$"), r"Цель: \1"),
    (re.compile(r"^Waiting for (.+)\.$"), r"Ожидание: \1."),
    (re.compile(r"^Confirmed (.+)\.$"), r"Подтверждено: \1."),
    (re.compile(r"^tspiot version (.+)$"), r"Версия tspiot \1"),
    (re.compile(r"^KKT driver version (.+)$"), r"Версия драйвера ККТ \1"),
    (re.compile(r"^gismt cert: (.+)$"), r"Сертификат ГИС МТ: \1"),
    (re.compile(r"^Publishing KKT driver v(.+)$"), r"Публикация драйвера ККТ версии \1"),
    (re.compile(r"^published (.+)$"), r"Опубликовано: \1"),
    (re.compile(r"^Existing (.+) not found, installing (.+)$"), r"Существующий файл \1 не найден; устанавливается \2"),
    (re.compile(r"^Workflow failed with innodb_force_recovery enabled; attempting emergency cleanup\.$"), r"Сбой при включённом innodb_force_recovery; выполняется аварийная очистка."),
    (re.compile(r"^Emergency recovery cleanup failed: (.+)$"), r"Аварийная очистка после восстановления завершилась ошибкой: \1"),
    (re.compile(r"^Stopping ukmclient before database operations\.$"), r"Остановка ukmclient перед операциями с БД."),
    (re.compile(r"^ukmclient is still running after service stop, terminating residual processes\.$"), r"ukmclient не остановился; завершаются оставшиеся процессы."),
    (re.compile(r"^ukmclient is still running after TERM, sending SIGKILL\.$"), r"ukmclient не остановился после TERM; отправляется SIGKILL."),
    (re.compile(r"^Waiting for MySQL to become ready\.$"), r"Ожидание готовности MySQL."),
    (re.compile(r"^Waiting for MySQL to stop completely\.$"), r"Ожидание полной остановки MySQL."),
    (re.compile(r"^Waiting for ukmclient to start\.$"), r"Ожидание запуска ukmclient."),
    (re.compile(r"^Waiting for ukmclient to stop completely\.$"), r"Ожидание полной остановки ukmclient."),
)


def localize_message(message: str | None) -> str:
    """Вернуть понятное оператору русское сообщение, сохранив техданные."""
    if not message:
        return ""
    translated = _EXACT_MESSAGES.get(message, message)
    for pattern, replacement in _PATTERNS:
        if pattern.match(translated):
            translated = pattern.sub(replacement, translated)
            break
    return _translate_error_details(translated)


def _translate_error_details(message: str) -> str:
    replacements = (
        ("Remote command failed with exit status", "Удалённая команда завершилась с кодом"),
        ("Remote command timed out after", "Время ожидания удалённой команды истекло через"),
        ("Operation cancelled by user.", "Операция отменена пользователем."),
        ("SSH client is not connected.", "SSH-клиент не подключён."),
        ("SSH transport is not active.", "SSH-транспорт не активен."),
        ("stdout:", "стандартный вывод:"),
        ("stderr:", "стандартный вывод ошибок:"),
        ("Timed out while waiting for", "Истекло время ожидания:"),
        ("Unknown workflow step:", "Неизвестный шаг восстановления БД:"),
        ("Unknown TS PIoT step:", "Неизвестный шаг ТС ПИоТ:"),
        ("Unknown MySQL rebuild steps:", "Неизвестные шаги пересборки MySQL:"),
        ("Cash node returned an invalid current time:", "Касса вернула некорректное текущее время:"),
        ("KKT time verification failed:", "Проверка времени ККТ не пройдена:"),
        ("KKT serial exchange did not return a verification response.", "Обмен с ККТ по последовательному порту не вернул ответ проверки."),
        ("KKT serial exchange returned malformed XML.", "ККТ вернула некорректный XML."),
        ("KKT returned an invalid date and time:", "ККТ вернула некорректные дату и время:"),
        ("Config file not found:", "Файл конфигурации не найден:"),
        ("Missing table", "Отсутствует секция"),
        ("in config.", "в конфигурации."),
        ("Missing or empty value:", "Отсутствует или пусто значение:"),
        ("Value must be string:", "Значение должно быть строкой:"),
        ("key_filename must be a string path.", "key_filename должен быть строковым путём."),
        ("remote_mysql_dir must be an absolute non-root path.", "remote_mysql_dir должен быть абсолютным путём, отличным от корня."),
        ("remote_mysql_var_dir must be exactly <remote_mysql_dir>/var.", "remote_mysql_var_dir должен быть равен <remote_mysql_dir>/var."),
        ("remote_tmp_dir must be an absolute non-root path.", "remote_tmp_dir должен быть абсолютным путём, отличным от корня."),
        ("Section [mysqld] not found in /etc/my.cnf", "Секция [mysqld] не найдена в /etc/my.cnf"),
        ("Invalid latest.json:", "Некорректный latest.json:"),
        ("Invalid version in latest.json:", "Некорректная версия в latest.json:"),
        ("At least one workflow step must be provided.", "Нужно выбрать хотя бы один шаг восстановления БД."),
        ("At least one TS PIoT step must be provided.", "Нужно выбрать хотя бы один шаг ТС ПИоТ."),
        ("dbdump.sh completed without success marker.", "dbdump.sh завершился без признака успешного выполнения."),
        ("dbrestore.sh completed without success marker.", "dbrestore.sh завершился без признака успешного выполнения."),
        ("Downloaded SQL dump checksum does not match the remote file.", "Контрольная сумма скачанного SQL-дампа не совпадает с файлом на кассе."),
        ("Refusing to replace datadir: the SQL dump was not verified in this workflow session.", "Замена datadir запрещена: SQL-дамп не был проверен в текущем запуске."),
        ("Could not read SHA-256 for", "Не удалось получить SHA-256 для"),
        ("Invalid empty datadir archive:", "Некорректный архив пустого datadir:"),
        ("Empty datadir archive has no files:", "Архив пустого datadir не содержит файлов:"),
        ("Empty datadir archive may contain only the var directory.", "Архив пустого datadir может содержать только каталог var."),
        ("Empty datadir archive contains an unsafe link or path.", "Архив пустого datadir содержит небезопасную ссылку или путь."),
        ("Invalid dbrepair archive:", "Некорректный архив dbrepair:"),
        ("dbrepair archive contains an unsafe path or link.", "Архив dbrepair содержит небезопасный путь или ссылку."),
        ("dbrepair archive does not contain db.ini, dbdump.sh and dbrestore.sh in its root directory.", "В корне архива dbrepair отсутствуют db.ini, dbdump.sh или dbrestore.sh."),
        ("SFTP client is not connected.", "SFTP-клиент не подключён."),
        ("Preflight failed:", "Предварительная проверка не пройдена:"),
        ("MySQL rebuild failed:", "Пересборка MySQL не выполнена:"),
        ("Check:", "Проверка:"),
        ("Source server:", "Сервер-источник:"),
        ("Source:", "Источник:"),
        ("Previous datadir kept at", "Предыдущий datadir сохранён в"),
        ("UKM schema restored:", "Схема UKM восстановлена:"),
    )
    for source, target in replacements:
        message = message.replace(source, target)
    return message
