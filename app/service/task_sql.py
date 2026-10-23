"""SQL leaf validation and transactional execution (no credentials in errors)."""

import sqlite3
import time

import psycopg

from app.service.errors import BusinessError, invalid


def validate_sql(step):
    policy = (step.get("options") or {}).get("sql")
    if step["step_type"] != "sql":
        if policy is not None:
            invalid("sql 配置仅适用于 SQL 节点", "steps.options.sql")
        return
    if (
        not isinstance(policy, dict)
        or policy.keys()
        - {"driver", "datasource", "username", "password", "statement", "timeout_seconds"}
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


def execute_sql(step):
    policy = step["options"]["sql"]
    timeout = policy.get("timeout_seconds", 30)
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
                cursor.execute(policy["statement"], prepare=True)
        else:
            deadline = time.monotonic() + timeout
            db = sqlite3.connect(policy["datasource"], timeout=timeout)
            try:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                with db:
                    db.execute(policy["statement"])
            finally:
                db.close()
    except (psycopg.Error, sqlite3.Error, ValueError, OSError):
        raise BusinessError(
            422,
            "SQL_EXECUTION_ERROR",
            "SQL 执行失败，事务已回滚；请检查数据源、权限、语句及超时配置",
        ) from None
    return {}
