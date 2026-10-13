"""Shared PostgreSQL connection configuration; credentials never enter CLI arguments."""

import psycopg

from app.config.settings import absolute, load_settings


def connection_options(admin: bool = False) -> dict:
    database = load_settings()["database"]
    source = database["admin"] if admin else database
    options = {
        "host": source["host"],
        "port": source["port"],
        "dbname": source["name"],
        "user": source["user"],
        "connect_timeout": database["connect_timeout"],
    }
    if admin and source.get("auth") == "peer":
        return options
    secret = absolute(source["password_file"])
    if secret.exists():
        if secret.stat().st_mode & 0o077:
            raise ValueError("数据库密码文件权限必须为 0600")
        options["password"] = secret.read_text().strip()
        if not options["password"]:
            raise ValueError("数据库密码文件不能为空")
    return options


def connect(admin: bool = False, autocommit: bool = False):
    return psycopg.connect(**connection_options(admin), autocommit=autocommit)


def check_database() -> None:
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
