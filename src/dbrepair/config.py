from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import posixpath
import re
import tomllib


class ConfigError(ValueError):
    """Raised when config.toml is missing required values or contains invalid paths."""


@dataclass(frozen=True)
class ConnectionConfig:
    host: str
    username: str
    password: str | None
    port: int = 22
    timeout: float = 20.0
    key_filename: Path | None = None
    use_sudo: bool = False
    sudo_password: str | None = None

    def resolved_sudo_password(self) -> str | None:
        return self.sudo_password or self.password


@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    password: str


@dataclass(frozen=True)
class ServiceCommands:
    mysql_stop: str = "/etc/init.d/mysql stop"
    mysql_start: str = "/etc/init.d/mysql start"
    ukmclient_stop: str = "/etc/init.d/ukmclient stop"
    ukmclient_start: str = "/etc/init.d/ukmclient start"


@dataclass(frozen=True)
class WorkflowPaths:
    dbrepair_archive: Path
    empty_datadir_archive: Path
    local_backup_dir: Path
    remote_tmp_dir: str = "/tmp"
    remote_dbrepair_dir_name: str = "dbrepair6700+"
    remote_mysql_dir: str = "/usr/local/mysql"
    remote_mysql_var_dir: str = "/usr/local/mysql/var"
    remote_mysql_backup_name: str = "mysql-db.tgz"
    remote_my_cnf: str = "/etc/my.cnf"
    remote_dump_filename: str = "ukmclient.sql"

    @property
    def remote_dbrepair_archive(self) -> str:
        return posixpath.join(self.remote_tmp_dir, self.dbrepair_archive.name)

    @property
    def remote_dbrepair_dir(self) -> str:
        return posixpath.join(self.remote_tmp_dir, self.remote_dbrepair_dir_name)

    @property
    def remote_db_ini(self) -> str:
        return posixpath.join(self.remote_dbrepair_dir, "db.ini")

    @property
    def remote_dbdump_script(self) -> str:
        return posixpath.join(self.remote_dbrepair_dir, "dbdump.sh")

    @property
    def remote_dbrestore_script(self) -> str:
        return posixpath.join(self.remote_dbrepair_dir, "dbrestore.sh")

    @property
    def remote_dump_file(self) -> str:
        return posixpath.join(self.remote_dbrepair_dir, self.remote_dump_filename)

    @property
    def remote_mysql_backup(self) -> str:
        return posixpath.join(self.remote_mysql_dir, self.remote_mysql_backup_name)

    @property
    def remote_empty_datadir_archive(self) -> str:
        return posixpath.join(self.remote_mysql_dir, self.empty_datadir_archive.name)


@dataclass(frozen=True)
class WebServerConfig:
    """Адрес веб-сервера дистрибутивов (HTTP, без авторизации). Устаревшее — см. DistributionConfig."""

    base_url: str = "http://192.168.20.229/UKM/"

    def normalized(self) -> str:
        url = self.base_url.strip()
        return url if url.endswith("/") else url + "/"


@dataclass(frozen=True)
class DistributionConfig:
    """Источник дистрибутивов ТС ПИоТ: HTTP-сервер или локальный каталог.

    Если задан local_dir — используется локальный каталог (файлы заливаются на
    кассу по SFTP). Иначе — base_url (касса качает файлы по HTTP напрямую).
    """

    base_url: str | None = None
    local_dir: Path | None = None


@dataclass(frozen=True)
class TsPiotConfig:
    """Настройки установки модуля ТС ПИоТ (источник файлов — веб-сервер)."""

    target_base: str = "/usr/local/ukmclient"
    data_dir_name: str = "data_tspiot"
    binary_name: str = "tspiot"
    gismt_cert_name: str = "gismt_cert.txt"
    owner: str | None = None

    def resolve_owner(self, target_base: str) -> str:
        if self.owner:
            return self.owner
        base = posixpath.basename(target_base.rstrip("/")) or "ukmclient"
        return f"{base}:{base}"


@dataclass(frozen=True)
class PublishConfig:
    """Доступ к веб-серверу для публикации дистрибутивов (SSH/SFTP)."""

    host: str
    username: str
    password: str | None = None
    port: int = 22
    ukm_dir: str = "/var/www/files/UKM"
    owner: str = "www-data:www-data"


@dataclass(frozen=True)
class AppConfig:
    connection: ConnectionConfig
    database: DatabaseConfig
    paths: WorkflowPaths
    services: ServiceCommands
    source_path: Path
    tspiot: TsPiotConfig | None = None
    webserver: WebServerConfig | None = None
    distribution: DistributionConfig | None = None
    publish: PublishConfig | None = None


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base_dir = path.parent

    connection_raw = _require_table(raw, "connection")
    database_raw = _require_table(raw, "database")
    paths_raw = _require_table(raw, "paths")
    services_raw = raw.get("services", {})

    dbrepair_archive = _resolve_path(base_dir, _require_str(paths_raw, "dbrepair_archive"))
    empty_datadir_archive = _resolve_path(base_dir, _require_str(paths_raw, "empty_datadir_archive"))
    local_backup_dir = _resolve_path(base_dir, _require_str(paths_raw, "local_backup_dir"))

    remote_dbrepair_dir_name = paths_raw.get("remote_dbrepair_dir_name") or _derive_dir_name(
        dbrepair_archive.name
    )

    connection = ConnectionConfig(
        host=_require_str(connection_raw, "host"),
        username=_require_str(connection_raw, "username"),
        password=_optional_str(connection_raw, "password"),
        port=int(connection_raw.get("port", 22)),
        timeout=float(connection_raw.get("timeout", 20.0)),
        key_filename=_optional_path(base_dir, connection_raw.get("key_filename")),
        use_sudo=bool(connection_raw.get("use_sudo", False)),
        sudo_password=_optional_str(connection_raw, "sudo_password"),
    )

    database = DatabaseConfig(
        name=_require_str(database_raw, "name"),
        password=_require_str(database_raw, "password"),
    )

    paths = WorkflowPaths(
        dbrepair_archive=dbrepair_archive,
        empty_datadir_archive=empty_datadir_archive,
        local_backup_dir=local_backup_dir,
        remote_tmp_dir=str(paths_raw.get("remote_tmp_dir", "/tmp")),
        remote_dbrepair_dir_name=str(remote_dbrepair_dir_name),
        remote_mysql_dir=str(paths_raw.get("remote_mysql_dir", "/usr/local/mysql")),
        remote_mysql_var_dir=str(paths_raw.get("remote_mysql_var_dir", "/usr/local/mysql/var")),
        remote_mysql_backup_name=str(paths_raw.get("remote_mysql_backup_name", "mysql-db.tgz")),
        remote_my_cnf=str(paths_raw.get("remote_my_cnf", "/etc/my.cnf")),
        remote_dump_filename=str(paths_raw.get("remote_dump_filename", "ukmclient.sql")),
    )

    services = ServiceCommands(
        mysql_stop=str(services_raw.get("mysql_stop", ServiceCommands.mysql_stop)),
        mysql_start=str(services_raw.get("mysql_start", ServiceCommands.mysql_start)),
        ukmclient_stop=str(services_raw.get("ukmclient_stop", ServiceCommands.ukmclient_stop)),
        ukmclient_start=str(services_raw.get("ukmclient_start", ServiceCommands.ukmclient_start)),
    )

    tspiot_raw = raw.get("tspiot")
    tspiot = _load_tspiot(tspiot_raw) if isinstance(tspiot_raw, dict) else None

    webserver_raw = raw.get("webserver")
    webserver = _load_webserver(webserver_raw) if isinstance(webserver_raw, dict) else None

    distribution_raw = raw.get("distribution")
    distribution = _load_distribution(base_dir, distribution_raw) if isinstance(distribution_raw, dict) else None

    publish_raw = raw.get("publish")
    publish = _load_publish(publish_raw) if isinstance(publish_raw, dict) else None

    return AppConfig(
        connection=connection,
        database=database,
        paths=paths,
        services=services,
        source_path=path,
        tspiot=tspiot,
        webserver=webserver,
        distribution=distribution,
        publish=publish,
    )


def _load_tspiot(raw: dict) -> TsPiotConfig:
    return TsPiotConfig(
        target_base=str(raw.get("target_base", "/usr/local/ukmclient")),
        data_dir_name=str(raw.get("data_dir_name", "data_tspiot")),
        binary_name=str(raw.get("binary_name", "tspiot")),
        gismt_cert_name=str(raw.get("gismt_cert_name", "gismt_cert.txt")),
        owner=_optional_str(raw, "owner"),
    )


def _load_webserver(raw: dict) -> WebServerConfig:
    return WebServerConfig(
        base_url=str(raw.get("base_url", "http://192.168.20.229/UKM/")),
    )


def _load_distribution(base_dir: Path, raw: dict) -> DistributionConfig:
    base_url = _optional_str(raw, "base_url")
    local_dir_raw = raw.get("local_dir")
    local_dir = _resolve_path(base_dir, local_dir_raw) if isinstance(local_dir_raw, str) and local_dir_raw.strip() else None
    return DistributionConfig(base_url=base_url, local_dir=local_dir)


def _load_publish(raw: dict) -> PublishConfig:
    return PublishConfig(
        host=_require_str(raw, "host"),
        username=_require_str(raw, "username"),
        password=_optional_str(raw, "password"),
        port=int(raw.get("port", 22)),
        ukm_dir=str(raw.get("ukm_dir", "/var/www/files/UKM")),
        owner=str(raw.get("owner", "www-data:www-data")),
    )


def override_host(config: AppConfig, host: str | None) -> AppConfig:
    candidate = (host or "").strip()
    if not candidate:
        return config
    return replace(config, connection=replace(config.connection, host=candidate))


def _require_table(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing table [{key}] in config.")
    return value


def _require_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or empty value: {key}")
    return value.strip()


def _optional_str(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Value must be string: {key}")
    return value.strip() or None


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _optional_path(base_dir: Path, raw_path: object) -> Path | None:
    if raw_path is None:
        return None
    if not isinstance(raw_path, str):
        raise ConfigError("key_filename must be a string path.")
    return _resolve_path(base_dir, raw_path)


def _derive_dir_name(archive_name: str) -> str:
    if archive_name.endswith(".tar.gz"):
        return archive_name[: -len(".tar.gz")]
    if archive_name.endswith(".tgz"):
        return archive_name[: -len(".tgz")]
    return Path(archive_name).stem


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_publish_password(config_path: str | Path, password: str) -> None:
    """Записать/обновить password в секции [publish] файла config.toml, сохраняя комментарии."""
    path = Path(config_path).expanduser()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    new_line = f'password = "{_toml_escape(password)}"'
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    password_re = re.compile(r"^\s*#?\s*password\s*=")

    out: list[str] = []
    in_publish = False
    inserted = False
    for line in lines:
        match = section_re.match(line)
        if match:
            if in_publish and not inserted:
                out.append(new_line)
                inserted = True
            in_publish = match.group(1).strip() == "publish"
            out.append(line)
            continue
        if in_publish and not inserted and password_re.match(line):
            out.append(new_line)
            inserted = True
            continue
        out.append(line)

    if in_publish and not inserted:
        out.append(new_line)
        inserted = True
    if not inserted:
        out.extend(["", "[publish]", new_line])

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
