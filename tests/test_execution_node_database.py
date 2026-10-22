"""Opt-in schema validation in rollback-only, isolated PostgreSQL schemas."""

import importlib.util
import os
from uuid import uuid4

import psycopg
import pytest
from lxml import etree

from app.config.settings import ROOT
from app.repository.database import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOTEST_DB_TESTS") != "1",
    reason="Set AUTOTEST_DB_TESTS=1 for PostgreSQL checks",
)


@pytest.fixture
def schema_sync():
    spec = importlib.util.spec_from_file_location(
        "execution_schema_sync", ROOT / "scripts/sync_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database():
    connection = connect()
    try:
        name = f"execution_nodes_{uuid4().hex}"
        connection.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(name)))
        connection.execute(
            psycopg.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                psycopg.sql.Identifier(name)
            )
        )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def assert_node_schema(connection):
    nullable = dict(
        connection.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='task_steps'"
        ).fetchall()
    )
    assert all(nullable[key] == "YES" for key in ("interface_id", "file_id", "version"))
    assert all(nullable[key] == "NO" for key in ("step_type", "execution_scope", "headers"))
    result = connection.execute(
        "INSERT INTO task_steps(task_id,position,step_type,execution_scope,wait_milliseconds) "
        "VALUES (1,10,'wait','finally',1000) RETURNING interface_id,headers,raw_body"
    ).fetchone()
    assert result == (None, [], None)
    assert (
        connection.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname=current_schema() "
            "AND indexname IN ('ix_task_steps_task_scope_parent_position',"
            "'ix_task_run_steps_run_scope_parent_position')"
        ).fetchone()[0]
        == 2
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
            "AND contype IN ('f','c') AND conrelid IN "
            "('task_steps'::regclass,'task_runs'::regclass,'task_run_steps'::regclass,"
            "'task_run_parameters'::regclass,'task_requests'::regclass)"
        ).fetchone()[0]
        == 0
    )


def test_fresh_schema_and_repeated_upgrade(database, schema_sync):
    statements = list(schema_sync.load_statements(ROOT / "db"))
    assert any(key.startswith("execution-nodes-") for key, _ in statements)
    assert len({key for key, _ in statements}) == len(statements)
    for _ in range(2):
        for _, sql in statements:
            database.execute(sql)
    assert_node_schema(database)


def test_upgrade_existing_tables_with_rows(database, schema_sync, tmp_path):
    tree = etree.parse(str(ROOT / "db/020_task_compose.xml"))
    additions = {
        "task_steps": {
            "step_type",
            "execution_scope",
            "parent_step_id",
            "wait_milliseconds",
            "max_concurrency",
            "headers",
            "raw_body",
        },
        "task_run_steps": {"step_type", "execution_scope", "parent_run_step_id", "parallel_stats"},
        "task_run_parameters": {"published"},
        "task_runs": {
            "execution_stage",
            "primary_error_code",
            "primary_error_message",
            "cleanup_status",
        },
    }
    for table in tree.findall("./tables/table"):
        for column in table.findall("./columns/column"):
            if column.get("name") in additions.get(table.get("name"), set()):
                column.getparent().remove(column)
            elif table.get("name") == "task_steps" and column.get("name") in (
                "interface_id",
                "file_id",
                "version",
            ):
                column.set("nullable", "false")
        for index in table.findall("./indexes/index"):
            if "scope_parent" in index.get("name"):
                index.getparent().remove(index)
    tree.write(str(tmp_path / "020_task_compose.xml"))
    for _, sql in schema_sync.load_statements(tmp_path):
        database.execute(sql)
    database.execute(
        "INSERT INTO task_steps(task_id,position,interface_id,file_id,version) VALUES (1,1,1,1,1)"
    )
    database.execute("INSERT INTO task_runs(task_id,project_id,config_snapshot) VALUES (1,1,'{}')")
    database.execute("INSERT INTO task_run_steps(run_id,step_id,position) VALUES (1,1,1)")
    database.execute(
        "INSERT INTO task_run_parameters(run_id,parameter_id,name,position,source_type,"
        "value_type,sensitive,status) VALUES (1,1,'token',1,'rest','string',false,'pending')"
    )
    upgrade_dir = tmp_path / "upgrade"
    upgrade_dir.mkdir()
    (upgrade_dir / "050_task_execution_nodes.xml").write_bytes(
        (ROOT / "db/050_task_execution_nodes.xml").read_bytes()
    )
    for _ in range(2):
        for _, sql in schema_sync.load_statements(upgrade_dir):
            database.execute(sql)
    assert_node_schema(database)
    assert database.execute("SELECT published FROM task_run_parameters").fetchone() == (False,)


def test_run_uuid_backfills_unique_values_without_changing_ids(database, schema_sync, tmp_path):
    database.execute(
        "CREATE TABLE task_run_steps(id integer PRIMARY KEY, run_id integer, step_id integer)"
    )
    database.execute("INSERT INTO task_run_steps VALUES (10,1,2),(11,1,3),(12,2,2)")
    (tmp_path / "060_step_run_logging.xml").write_bytes(
        (ROOT / "db/060_step_run_logging.xml").read_bytes()
    )
    for _, sql in schema_sync.load_statements(tmp_path):
        database.execute(sql)
    rows = database.execute(
        "SELECT id,run_id,step_id,run_uuid FROM task_run_steps ORDER BY id"
    ).fetchall()
    assert [r[:3] for r in rows] == [(10, 1, 2), (11, 1, 3), (12, 2, 2)]
    assert len({r[3] for r in rows}) == 3
    assert all(r[3].version == 4 for r in rows)
    for _, sql in schema_sync.load_statements(tmp_path):
        database.execute(sql)
    assert (
        database.execute(
            "SELECT id,run_id,step_id,run_uuid FROM task_run_steps ORDER BY id"
        ).fetchall()
        == rows
    )
    fresh = database.execute(
        "INSERT INTO task_run_steps(id) VALUES(13) RETURNING run_uuid"
    ).fetchone()[0]
    assert fresh.version == 4 and fresh not in {r[3] for r in rows}
    assert (
        database.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid='task_run_steps'::regclass AND contype='f'"
        ).fetchone()[0]
        == 0
    )
