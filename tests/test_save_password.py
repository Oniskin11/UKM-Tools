import tomllib

from dbrepair.config import save_publish_password


def _read(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_update_existing_password(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[connection]\n"
        'password = "conn"\n\n'
        "[publish]\n"
        "host = \"h\"\n"
        'password = "old"  # комментарий\n'
        'owner = "www-data:www-data"\n',
        encoding="utf-8",
    )
    save_publish_password(cfg, "new-secret")
    data = _read(cfg)
    assert data["publish"]["password"] == "new-secret"
    assert data["connection"]["password"] == "conn"  # чужой пароль не тронут
    assert data["publish"]["owner"] == "www-data:www-data"


def test_insert_when_missing(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[publish]\n"
        'host = "h"\n'
        'username = "root"\n',
        encoding="utf-8",
    )
    save_publish_password(cfg, "abc")
    data = _read(cfg)
    assert data["publish"]["password"] == "abc"
    assert data["publish"]["host"] == "h"


def test_escapes_special_chars(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[publish]\nhost = "h"\n', encoding="utf-8")
    secret = 'a"b\\c'
    save_publish_password(cfg, secret)
    assert _read(cfg)["publish"]["password"] == secret


def test_update_multiple_sections_and_types(tmp_path):
    from dbrepair.config import update_config_sections

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[distribution]\n"
        'base_url = "http://old/"\n'
        "# local_dir = \"x\"\n",
        encoding="utf-8",
    )
    update_config_sections(
        cfg,
        {
            "distribution": {"base_url": "http://new/UKM/", "local_dir": ""},
            "publish": {"host": "h", "port": 2222},
        },
    )
    data = _read(cfg)
    assert data["distribution"]["base_url"] == "http://new/UKM/"
    assert data["distribution"]["local_dir"] == ""
    assert data["publish"]["host"] == "h"
    assert data["publish"]["port"] == 2222  # int без кавычек
