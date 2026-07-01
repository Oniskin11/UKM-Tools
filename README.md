# DbRepair

CLI-утилита на Python для автоматизации восстановления БД кассы по SSH/SFTP.

Что делает:

- загружает архив `dbrepair6700+.tgz` на кассу и распаковывает его;
- обновляет `db.ini` с `DBNAME` и `DBPASSWORD`;
- делает резервную копию `/usr/local/mysql/var` в `mysql-db.tgz`;
- включает и затем отключает `innodb_force_recovery=6` в `/etc/my.cnf`;
- запускает `dbdump.sh` и проверяет сообщение `SUCCESS: DB dump complete`;
- сохраняет копию `ukmclient.sql` и на кассе, и локально;
- удаляет `/usr/local/mysql/var`, разворачивает пустой datadir из `mysql5-datadir-empty_46+.tgz`;
- запускает `dbrestore.sh` и проверяет сообщение `SUCCESS: DB restore complete`;
- запускает `ukmclient`.

## Установка

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Настройка

1. Скопируйте `config.example.toml` в `config.toml`.
2. Укажите доступ к кассе и локальные пути к архивам.
3. Если вы подключаетесь не под `root`, включите `use_sudo = true`.

## Запуск

```powershell
python -m dbrepair.cli --config .\config.toml
```

GUI-режим:

```powershell
python -m dbrepair.cli --gui --config .\config.toml
```

или

```powershell
dbrepair-gui
```

В GUI доступны:

- список адресов касс в одном окне;
- отдельная вкладка на каждую кассу;
- кнопка запуска всех шагов сразу по всем кассам;
- кнопка отмены по текущей кассе и кнопка отмены всех активных запусков;
- отдельная кнопка полного прогона и отдельные кнопки на каждый шаг внутри вкладки кассы;
- статус `Не запускался / Выполняется / Успех / Ошибка` для каждого шага;
- отдельный журнал выполнения для каждой кассы.

После запуска:

- локальная копия `ukmclient.sql` будет сохранена в каталоге `local_backup_dir/<host>/`;
- лог выполнения будет создан в `logs/`, с уникальным именем на каждую кассу и каждый запуск.

## Ограничения

- утилита предполагает доступ по SSH/SFTP;
- для системных команд нужны права `root` или рабочий `sudo`;
- `dbdump.sh` и `dbrestore.sh` должны находиться в распакованном каталоге `dbrepair6700+`.
