"""Opt-in database verification. All test rows are rolled back."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.repository.database import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOTEST_DB_TESTS") != "1",
    reason="Set AUTOTEST_DB_TESTS=1 after synchronizing the database",
)


@pytest.fixture
def database():
    connection = connect()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def insert_project(connection):
    return connection.execute(
        "INSERT INTO projects(name) VALUES (%s) RETURNING id", (f"schema-test-{uuid4()}",)
    ).fetchone()[0]


def insert_version(connection, file_id, version):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "测试接口", "version": "1.0.0"},
        "paths": {"/orders": {"get": {"responses": {"200": {"description": "成功"}}}}},
    }
    import yaml

    connection.execute(
        """INSERT INTO yaml_versions
           (file_id,version,filename,interface_count,openapi_version,title,api_version,
            original_content,parsed_document)
           VALUES (%s,%s,'orders.yaml',1,'3.0.3','测试接口','1.0.0',%s,%s)""",
        (file_id, version, yaml.safe_dump(document), Jsonb(document)),
    )
    return connection.execute(
        """INSERT INTO api_interfaces(file_id,version,method,path,responses)
           VALUES (%s,%s,'GET','/orders',%s) RETURNING id""",
        (file_id, version, Jsonb({"200": {"description": "成功"}})),
    ).fetchone()[0]


def insert_file(connection, project_id, name="接口文件"):
    file_id = connection.execute(
        "INSERT INTO yaml_files(project_id,name) VALUES (%s,%s) RETURNING id",
        (project_id, name),
    ).fetchone()[0]
    interface_id = insert_version(connection, file_id, 1)
    return file_id, interface_id


def test_names_are_unique_in_the_required_scope(database):
    first_project = insert_project(database)
    second_project = insert_project(database)
    name = database.execute("SELECT name FROM projects WHERE id=%s", (first_project,)).fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation), database.transaction():
        database.execute("INSERT INTO projects(name) VALUES (%s)", (name,))
    insert_file(database, first_project)
    insert_file(database, second_project)  # Same file name allowed across projects.
    with pytest.raises(psycopg.errors.UniqueViolation), database.transaction():
        database.execute(
            "INSERT INTO yaml_files(project_id,name) VALUES (%s,'接口文件')", (first_project,)
        )
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute("INSERT INTO projects(name) VALUES ('   ')")


def test_relationships_are_not_enforced_by_database_foreign_keys(database):
    assert (
        database.execute(
            """SELECT count(*) FROM pg_constraint
           WHERE contype='f' AND connamespace=current_schema()::regnamespace
             AND conrelid IN ('projects'::regclass,'yaml_files'::regclass,
                             'yaml_versions'::regclass,'api_interfaces'::regclass)"""
        ).fetchone()[0]
        == 0
    )
    file_id = database.execute(
        """INSERT INTO yaml_files(project_id,name,current_version)
           VALUES (2147483647,'程序负责关联校验',999) RETURNING id"""
    ).fetchone()[0]
    assert file_id > 0
    insert_version(database, file_id, 1)
    database.execute("UPDATE yaml_files SET current_version=1000 WHERE id=%s", (file_id,))


def test_new_versions_preserve_immutable_definitions(database):
    project_id = insert_project(database)
    file_id, interface_id = insert_file(database, project_id)
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute("UPDATE yaml_versions SET title='changed' WHERE file_id=%s", (file_id,))
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute("UPDATE api_interfaces SET summary='changed' WHERE id=%s", (interface_id,))
    second_interface = insert_version(database, file_id, 2)
    database.execute("UPDATE yaml_files SET current_version=2 WHERE id=%s", (file_id,))
    assert second_interface != interface_id
    assert database.execute(
        "SELECT version FROM yaml_versions WHERE file_id=%s ORDER BY version", (file_id,)
    ).fetchall() == [(1,), (2,)]
    with pytest.raises(psycopg.errors.UniqueViolation), database.transaction():
        database.execute(
            """INSERT INTO api_interfaces(file_id,version,method,path,responses)
               SELECT file_id,version,method,path,responses FROM api_interfaces WHERE id=%s""",
            (interface_id,),
        )


def test_deleting_parent_does_not_cascade_and_program_can_cleanup(database):
    project_id = insert_project(database)
    first_file, _ = insert_file(database, project_id, "文件一")
    second_file, _ = insert_file(database, project_id, "文件二")
    database.execute("DELETE FROM yaml_files WHERE id=%s", (first_file,))
    assert (
        database.execute(
            "SELECT count(*) FROM yaml_versions WHERE file_id=%s", (first_file,)
        ).fetchone()[0]
        == 1
    )
    assert (
        database.execute(
            "SELECT count(*) FROM api_interfaces WHERE file_id=%s", (first_file,)
        ).fetchone()[0]
        == 1
    )
    database.execute("DELETE FROM projects WHERE id=%s", (project_id,))
    assert (
        database.execute("SELECT count(*) FROM yaml_files WHERE id=%s", (second_file,)).fetchone()[
            0
        ]
        == 1
    )
    # Program-managed cleanup must explicitly remove children in the same transaction.
    for table in ["api_interfaces", "yaml_versions", "yaml_files"]:
        column = "id" if table == "yaml_files" else "file_id"
        query = psycopg.sql.SQL("DELETE FROM {} WHERE {}=ANY(%s)").format(
            psycopg.sql.Identifier(table), psycopg.sql.Identifier(column)
        )
        database.execute(query, ([first_file, second_file],))
        count_query = psycopg.sql.SQL("SELECT count(*) FROM {} WHERE {}=ANY(%s)").format(
            psycopg.sql.Identifier(table), psycopg.sql.Identifier(column)
        )
        assert database.execute(count_query, ([first_file, second_file],)).fetchone()[0] == 0


def test_import_storage_constraints(database):
    project_id = insert_project(database)
    file_id, interface_id = insert_file(database, project_id)
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute(
            """INSERT INTO api_interfaces(file_id,version,method,path,responses,parameters)
               VALUES (%s,1,'POST','/invalid','{"200":{}}','{}')""",
            (file_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute(
            """INSERT INTO yaml_versions
               (file_id,version,filename,interface_count,openapi_version,title,api_version,
                original_content,parsed_document)
               VALUES (%s,2,'large.yaml',1,'3.0.3','title','1.0',%s,'{}')""",
            (file_id, "x" * (5242880 + 1)),
        )
    assert (
        database.execute("SELECT path FROM api_interfaces WHERE id=%s", (interface_id,)).fetchone()[
            0
        ]
        == "/orders"
    )


def test_business_ids_are_generated_as_positive_integers(database):
    first_project = insert_project(database)
    second_project = insert_project(database)
    file_id, interface_id = insert_file(database, first_project)
    assert all(
        isinstance(value, int) and value > 0
        for value in [first_project, second_project, file_id, interface_id]
    )
    assert second_project > first_project
    columns = database.execute(
        """SELECT table_name, data_type, is_identity, identity_generation
           FROM information_schema.columns
           WHERE table_schema=current_schema()
             AND table_name IN ('projects','yaml_files','api_interfaces')
             AND column_name='id' ORDER BY table_name"""
    ).fetchall()
    assert columns == [
        ("api_interfaces", "integer", "YES", "BY DEFAULT"),
        ("projects", "integer", "YES", "BY DEFAULT"),
        ("yaml_files", "integer", "YES", "BY DEFAULT"),
    ]
    with pytest.raises(psycopg.errors.CheckViolation), database.transaction():
        database.execute(
            "INSERT INTO projects(id,name) VALUES (-1,%s)", (f"negative-id-{uuid4()}",)
        )


def test_fresh_schema_creates_integer_identity_ids(database):
    import importlib.util

    from app.config.settings import ROOT

    schema_name = f"identity_validation_{uuid4().hex}"
    database.execute(
        psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema_name))
    )
    database.execute(
        psycopg.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
            psycopg.sql.Identifier(schema_name)
        )
    )
    spec = importlib.util.spec_from_file_location(
        "identity_schema_sync", ROOT / "scripts/sync_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = list(module.load_statements(ROOT / "db"))
    for _, statement in statements:
        database.execute(statement)
    assert database.execute(
        "INSERT INTO projects(name) VALUES ('first'),('second') RETURNING id"
    ).fetchall() == [(1,), (2,)]
    # The current baseline defines relationships in application code and contains no FK migration.
    assert database.execute(
        "SELECT count(*) FROM pg_constraint WHERE contype='f' "
        "AND connamespace=current_schema()::regnamespace"
    ).fetchone()[0] == 0
