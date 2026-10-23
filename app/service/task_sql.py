"""SQL leaf validation and transactional execution (no credentials in errors)."""

import sqlite3
import time

import psycopg

from app.service.errors import BusinessError, invalid


def validate_sql(step, declarations=None):
    policy = (step.get("options") or {}).get("sql")
    if step["step_type"] != "sql":
        if policy is not None:
            invalid("sql 配置仅适用于 SQL 节点", "steps.options.sql")
        return
    if (
        not isinstance(policy, dict)
        or policy.keys()
        - {
            "driver",
            "datasource",
            "username",
            "password",
            "statement",
            "timeout_seconds",
            "parameters",
        }
        or policy.get("driver") not in {"postgresql", "sqlite"}
        or any(
            not isinstance(policy.get(k), str) or not policy[k].strip()
            for k in ("datasource", "statement")
        )
        or any(not isinstance(policy.get(k, ""), str) for k in ("username", "password"))
        or type(policy.get("timeout_seconds", 30)) is not int
        or not 1 <= policy.get("timeout_seconds", 30) <= 300
    ):
        invalid("SQL 须配置 postgresql/sqlite、数据源、SQL 及 1～300 秒超时", "steps.options.sql")
    if policy["driver"] == "sqlite" and (policy.get("username") or policy.get("password")):
        invalid("SQLite 不支持用户名/密码", "steps.options.sql")

    parameters = policy.get("parameters", [])
    if (
        not isinstance(parameters, list)
        or len(parameters) > 100
        or any(not isinstance(name, str) or not name.strip() for name in parameters)
    ):
        invalid("SQL parameters 须为至多 100 个全局参数名", "steps.options.sql.parameters")
    if declarations is not None and set(parameters) - declarations.keys():
        invalid("SQL 引用了未声明参数", "steps.options.sql.parameters")


def execute_sql(step, values=None):
    policy = step["options"]["sql"]
    timeout = policy.get("timeout_seconds", 30)
    values = values or {}
    names = policy.get("parameters", [])
    if set(names) - values.keys():
        raise BusinessError(422, "PARAMETER_NOT_AVAILABLE", "SQL 依赖的参数尚未赋值")
    parameters = tuple(values[name] for name in names)
    if any(type(value) not in (str, int, float, bool, type(None)) for value in parameters):
        raise BusinessError(422, "SQL_PARAMETER_INVALID", "SQL 参数须为标量或 null")
    try:
        if policy["driver"] == "postgresql":
            credentials = {k: policy[k] for k in ("password",) if k in policy}
            if "username" in policy:
                credentials["user"] = policy["username"]
            with (
                psycopg.connect(policy["datasource"], connect_timeout=timeout, **credentials) as db,
                db.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)", (str(timeout * 1000),)
                )
                if parameters:
                    cursor.execute(policy["statement"], parameters, prepare=True)
                else:
                    cursor.execute(policy["statement"], prepare=True)
        else:
            deadline = time.monotonic() + timeout
            db = sqlite3.connect(policy["datasource"], timeout=timeout)
            try:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                with db:
                    db.execute(policy["statement"], parameters)
            finally:
                db.close()
    except (psycopg.Error, sqlite3.Error, ValueError, OSError, OverflowError, TypeError):
        raise BusinessError(
            422,
            "SQL_EXECUTION_ERROR",
            "SQL 执行失败，事务已回滚；请检查数据源、权限、语句及超时配置",
        ) from None
    return {}
