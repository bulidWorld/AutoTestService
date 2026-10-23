import sqlite3

import pytest

from app.service.errors import BusinessError
from app.service.task_config import validate_config
from app.service.task_import import convert, parse_upload
from app.service.task_sql import execute_sql, validate_sql


def sql_step(datasource, statement, **extra):
    return {
        "id": 1,
        "position": 1,
        "execution_scope": "main",
        "step_type": "sql",
        "options": {
            "sql": {
                "driver": "sqlite",
                "datasource": str(datasource),
                "statement": statement,
                **extra,
            }
        },
    }


def test_import_and_validate_sql(tmp_path):
    xml = f"""<task name="sql"><taskStep order="1"><sql driver="sqlite">
    <datasource>{tmp_path / "test.db"}</datasource>
    <statement><![CDATA[CREATE TABLE test (id INTEGER)]]></statement>
    </sql></taskStep><finally><taskStep order="1"><sql driver="postgresql">
    <datasource>host=localhost dbname=test</datasource><username>tester</username>
    <password>secret</password><statement>DELETE FROM test</statement>
    </sql></taskStep></finally></task>"""
    config = convert(parse_upload("task.xml", xml.encode()), "http://localhost", None)
    for index, step in enumerate(config["steps"]):
        step["id"] = index + 1
    validate_config(config)
    assert config["steps"][1]["options"]["sql"]["password"] == "secret"
    execute_sql(config["steps"][0])
    with sqlite3.connect(tmp_path / "test.db") as db:
        assert db.execute("SELECT count(*) FROM test").fetchone() == (0,)


def test_commit_rollback_and_sanitized_error(tmp_path):
    path = tmp_path / "test.db"
    execute_sql(sql_step(path, "CREATE TABLE test (id INTEGER UNIQUE)"))
    execute_sql(sql_step(path, "INSERT INTO test VALUES (1)"))
    with pytest.raises(BusinessError) as error:
        execute_sql(sql_step(path, "INSERT INTO test VALUES (1)", password="SECRET"))
    assert error.value.payload["code"] == "SQL_EXECUTION_ERROR"
    assert "SECRET" not in str(error.value.payload)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM test").fetchall() == [(1,)]


@pytest.mark.parametrize(
    "patch",
    [
        {"driver": "mysql"},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"statement": " "},
        {"username": "user"},
        {"unknown": True},
    ],
)
def test_invalid_sql(patch):
    step = sql_step(":memory:", "SELECT 1")
    step["options"]["sql"].update(patch)
    with pytest.raises(BusinessError):
        validate_sql(step)


def test_postgresql_credentials_and_transaction(monkeypatch):
    from unittest.mock import MagicMock

    connect = MagicMock()
    monkeypatch.setattr("app.service.task_sql.psycopg.connect", connect)
    step = sql_step(
        "host=localhost dbname=test", "UPDATE test SET id=2", username="tester", password="secret"
    )
    step["options"]["sql"]["driver"] = "postgresql"
    execute_sql(step)
    connect.assert_called_once_with(
        "host=localhost dbname=test", connect_timeout=30, user="tester", password="secret"
    )
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_args_list[-1].args == ("UPDATE test SET id=2",)
    assert cursor.execute.call_args.kwargs == {"prepare": True}
    assert connect.return_value.__exit__.call_args.args == (None, None, None)


def test_sqlite_execution_timeout():
    with pytest.raises(BusinessError) as error:
        execute_sql(
            sql_step(
                ":memory:",
                """WITH RECURSIVE n(x) AS (
            VALUES(1) UNION ALL SELECT x+1 FROM n
        ) SELECT sum(x) FROM n""",
                timeout_seconds=1,
            )
        )
    assert error.value.payload["code"] == "SQL_EXECUTION_ERROR"


def test_non_sql_rejects_sql_options():
    step = sql_step(":memory:", "SELECT 1")
    step["step_type"] = "wait"
    with pytest.raises(BusinessError):
        validate_sql(step)


def test_sql_before_compute_preview(tmp_path):
    from app.service.task_execution import preview

    path = tmp_path / "preview.db"
    config = {
        "name": "preview",
        "base_url": "http://localhost",
        "steps": [
            sql_step(path, "CREATE TABLE test (id INTEGER)"),
            {
                "id": 2,
                "position": 2,
                "step_type": "compute",
                "execution_scope": "main",
                "options": {"expressions": [{"name": "result", "expression": "42"}]},
            },
        ],
        "global_parameters": [
            {
                "name": "result",
                "source_type": "expression",
                "value_type": "integer",
                "sensitive": False,
                "expression_source": {"step_id": 2},
            }
        ],
    }
    validate_config(config)
    preview(config, "result")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM test").fetchall() == []


def test_api_sql_schema():
    import yaml
    from jsonschema import Draft4Validator, RefResolver

    with open("docs/ rest-api/rest-task.yaml") as stream:
        contract = yaml.safe_load(stream)
    schema = contract["components"]["schemas"]["StepInput"]
    Draft4Validator(schema, resolver=RefResolver.from_schema(contract)).validate(
        {
            "step_type": "sql",
            "execution_scope": "main",
            "position": 10,
            "options": {
                "sql": {
                    "driver": "postgresql",
                    "datasource": "host=localhost",
                    "username": "test",
                    "password": "secret",
                    "statement": "SELECT 1",
                }
            },
        }
    )
