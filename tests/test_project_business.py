"""Contract-backed API tests, with optional PostgreSQL isolation."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft4Validator

from app.config.settings import ROOT
from app.main import create_app
from app.repository.database import connect
from app.service.errors import BusinessError
from app.service.openapi_import import MAX_BYTES, parse_upload, redact

EXAMPLES = ROOT / "context/requirement/1-1.support_mang_proj/examples"
SPEC = yaml.safe_load((ROOT / "docs/rest-project.yaml").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump({"logging": {"directory": str(tmp_path / "logs")}}))
    monkeypatch.setenv("APP_CONFIG", str(config))
    with TestClient(create_app()) as client:
        yield client


def upload(client, path, filename="valid.yaml", name=None):
    data = {"name": name} if name is not None else {}
    return client.post(
        path,
        data=data,
        files={"file": (filename, (EXAMPLES / filename).read_bytes(), "application/yaml")},
    )


def check(response, schema, status=200):
    assert response.status_code == status, response.text
    Draft4Validator(SPEC).evolve(schema={"$ref": "#/components/schemas/" + schema}).validate(
        response.json()
    )
    return response.json()


def test_sample_nested_schema_and_parameter_override():
    doc = yaml.safe_load((EXAMPLES / "valid.yaml").read_text())
    doc["paths"]["/orders/{order_id}"]["parameters"] = [
        {"in": "path", "name": "order_id", "required": True, "schema": {"type": "string"}}
    ]
    parsed = parse_upload("orders.yml", yaml.safe_dump(doc).encode())
    assert len(parsed["interfaces"]) == 2
    get = next(i for i in parsed["interfaces"] if i["method"] == "GET")
    assert get["parameters"][0]["schema"]["type"] == "integer"
    schema = next(i for i in parsed["interfaces"] if i["method"] == "POST")["request_body"][
        "schema"
    ]
    assert schema["properties"]["items"]["items"]["properties"]["price"]["type"] == "number"


@pytest.mark.parametrize(
    "content,code",
    [
        (b"", "INVALID_YAML"),
        (b"paths: [", "INVALID_YAML"),
        (b"openapi: 3.1.0", "INVALID_OPENAPI"),
        (b"openapi: 3.0.3\nopenapi: 3.0.2", "INVALID_YAML"),
        ((EXAMPLES / "invalid.yaml").read_bytes(), "UNSUPPORTED_SCHEMA"),
        (b"x" * (MAX_BYTES + 1), "FILE_TOO_LARGE"),
    ],
)
def test_import_rejections_have_locations(content, code):
    with pytest.raises(BusinessError) as exc:
        parse_upload("test.yaml", content)
    assert exc.value.payload["code"] == code
    assert exc.value.payload["issues"][0]["location"]


@pytest.mark.parametrize(
    "modify",
    [
        lambda d: d["components"]["schemas"]["Order"].update({"oneOf": [{"type": "string"}]}),
        lambda d: d["paths"]["/orders"]["post"]["requestBody"].update(
            {"content": {"text/plain": {"schema": {"type": "string"}}}}
        ),
        lambda d: d["paths"]["/orders"]["post"].update(
            {"parameters": [{"name": "session", "in": "cookie", "schema": {"type": "string"}}]}
        ),
        lambda d: d["components"]["schemas"].update(
            {"Order": {"$ref": "#/components/schemas/Missing"}}
        ),
    ],
)
def test_unsupported_structures(modify):
    document = yaml.safe_load((EXAMPLES / "valid.yaml").read_text())
    modify(document)
    with pytest.raises(BusinessError):
        parse_upload("test.yaml", yaml.safe_dump(document).encode())


def test_sensitive_values_are_redacted():
    value = {
        "parameters": [
            {"name": "Authorization", "schema": {"type": "string", "example": "PRIVATE_AUTH"}}
        ],
        "properties": {"password": {"type": "string", "default": "PRIVATE_PASSWORD"}},
        "example": {"token": "PRIVATE_TOKEN"},
        "servers": [{"url": "https://user:PRIVATE_PASSWORD@example.com?token=PRIVATE_TOKEN"}],
    }
    safe = redact(value)
    assert "PRIVATE_" not in str(safe)
    assert safe["properties"]["password"]["type"] == "string"
    assert value != safe


@pytest.mark.parametrize(
    "path,options",
    [
        ("/api/v1/projects?page=0", {}),
        ("/api/v1/projects?page_size=101", {}),
        ("/api/v1/projects/0", {}),
        ("/api/v1/projects/1?confirm=false", {"method": "DELETE"}),
        ("/api/v1/projects/1", {"method": "PATCH", "json": {}}),
        ("/api/v1/projects", {"method": "POST", "json": {"name": "  "}}),
        ("/api/v1/projects", {"method": "POST", "json": {"name": "ok", "other": 1}}),
    ],
)
def test_request_validation_without_database(client, path, options):
    response = client.request(options.pop("method", "GET"), path, **options)
    check(response, "Error", 400)
    assert response.json()["issues"]
    assert response.headers["X-Request-ID"]


def test_browser_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "项目与接口管理" in response.text


@pytest.fixture
def isolated_database(monkeypatch):
    if os.environ.get("AUTOTEST_DB_TESTS") != "1":
        pytest.skip("Set AUTOTEST_DB_TESTS=1 to run PostgreSQL integration tests")
    schema = "business_test_" + uuid4().hex
    identifier = psycopg.sql.Identifier(schema)
    with connect() as db:
        db.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(identifier))
        db.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(identifier))
        spec = importlib.util.spec_from_file_location(
            "business_schema", ROOT / "scripts/sync_schema.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for _, sql in module.load_statements(ROOT / "db"):
            db.execute(sql)

    def test_connect():
        db = connect()
        db.execute(psycopg.sql.SQL("SET search_path TO {}, pg_catalog").format(identifier))
        db.commit()
        return db

    monkeypatch.setattr("app.repository.projects.connect", test_connect)
    try:
        yield test_connect
    finally:
        with connect() as db:
            db.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))


def test_complete_project_version_lifecycle(client, isolated_database):
    base = "/api/v1/projects"
    first = check(
        client.post(base, json={"name": "  项目一  ", "description": "初始"}), "Project", 201
    )
    pid = first["id"]
    project = f"{base}/{pid}"
    second = check(client.post(base, json={"name": "项目二"}), "Project", 201)
    check(client.post(base, json={"name": "项目一"}), "Error", 409)
    check(client.patch(project, json={"name": "项目二", "description": "不应保存"}), "Error", 409)
    detail = check(client.get(project), "ProjectDetail")
    assert detail["description"] == "初始" and detail["updated_at"] == first["updated_at"]
    assert check(client.get(base + "?name=项目一"), "ProjectPage")["total"] == 1
    check(client.patch(project, json={"description": "修改成功"}), "Project")
    imported = check(upload(client, project + "/yaml-files", name="订单"), "ImportResult", 201)
    fid = imported["file"]["id"]
    file = project + f"/yaml-files/{fid}"
    check(upload(client, project + "/yaml-files", name="订单"), "Error", 409)
    check(upload(client, f"{base}/{second['id']}/yaml-files", name="订单"), "ImportResult", 201)
    check(client.get(f"{base}/{second['id']}/yaml-files/{fid}"), "Error", 404)
    check(client.get(project + "/yaml-files"), "YamlFilePage")
    old = check(client.get(file + "/interfaces"), "InterfacePage")["items"]
    assert len(old) == 2
    for interface in old:
        check(client.get(file + "/interfaces/" + str(interface["id"])), "InterfaceDetail")
    content = check(client.get(file + "/versions/1/content"), "VersionContent")
    assert content["redacted"] and "PRIVATE_TOKEN" not in content["content"]
    unchanged = check(client.get(file), "YamlFile")
    check(upload(client, file + "/versions", "invalid.yaml"), "Error", 400)
    assert check(client.get(file), "YamlFile") == unchanged
    assert check(client.get(file + "/versions"), "YamlVersionPage")["total"] == 1
    updated = check(upload(client, file + "/versions", "updated.yaml"), "ImportResult", 201)
    assert updated["version"]["version"] == 2
    assert check(client.get(file + "/interfaces?version=1"), "InterfacePage")["items"] == old
    current = check(client.get(file + "/interfaces"), "InterfacePage")["items"]
    assert {i["path"] for i in current} == {"/orders/{order_id}", "/orders/{order_id}/cancel"}
    assert (
        check(client.get(file + "/interfaces?method=POST&path=cancel"), "InterfacePage")["total"]
        == 1
    )
    check(client.get(file + "/versions/2"), "YamlVersion")
    check(client.get(file + "/interfaces?version=999"), "Error", 404)
    check(client.get(file + "/interfaces/" + str(old[0]["id"])), "InterfaceDetail")
    assert check(client.get(file + "/deletion-preview"), "DeletionPreview")["interface_count"] == 4
    check(client.delete(file), "Error", 400)
    check(client.delete(file + "?confirm=true"), "DeletionResult")
    check(client.get(file), "Error", 404)
    check(client.delete(project + "?confirm=true"), "DeletionResult")
    with isolated_database() as db:
        assert (
            db.execute("SELECT count(*) FROM api_interfaces WHERE file_id=%s", (fid,)).fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT count(*) FROM yaml_versions WHERE file_id=%s", (fid,)).fetchone()[0]
            == 0
        )
    check(client.get(f"{base}/{second['id']}"), "ProjectDetail")


def test_concurrent_updates_get_distinct_versions(client, isolated_database):
    pid = client.post("/api/v1/projects", json={"name": "并发项目"}).json()["id"]
    project = f"/api/v1/projects/{pid}"
    fid = upload(client, project + "/yaml-files", name="接口").json()["file"]["id"]
    file = project + f"/yaml-files/{fid}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _: upload(client, file + "/versions", "updated.yaml"), range(2))
        )
    assert sorted(check(r, "ImportResult", 201)["version"]["version"] for r in responses) == [2, 3]
    preview = check(client.get(project + "/deletion-preview"), "DeletionPreview")
    assert preview["version_count"] == 3
    check(client.delete(project + "?confirm=true"), "DeletionResult")
    with isolated_database() as db:
        for table in ("yaml_files", "yaml_versions", "api_interfaces", "projects"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_recursive_internal_schema_preserves_cycle_reference():
    document = yaml.safe_load((EXAMPLES / "valid.yaml").read_text())
    document["components"]["schemas"]["Order"]["properties"]["parent"] = {
        "$ref": "#/components/schemas/Order"
    }
    parsed = parse_upload("recursive.yaml", yaml.safe_dump(document).encode())
    schema = next(i for i in parsed["interfaces"] if i["method"] == "POST")["request_body"][
        "schema"
    ]
    assert schema["properties"]["parent"] == {"$ref": "#/components/schemas/Order"}


def test_write_failure_rolls_back_entire_import(client, isolated_database, monkeypatch):
    from app.repository.projects import Projects

    pid = client.post("/api/v1/projects", json={"name": "事务测试"}).json()["id"]
    path = f"/api/v1/projects/{pid}/yaml-files"
    original = Projects.save_upload

    def fail_after_insert(self, *args):
        original(self, *args)
        raise RuntimeError("PRIVATE_PASSWORD must never appear in a response")

    with monkeypatch.context() as patch:
        patch.setattr(Projects, "save_upload", fail_after_insert)
        response = upload(client, path, name="失败导入")
        check(response, "Error", 500)
        assert "PRIVATE_PASSWORD" not in response.text
    assert client.get(path).json()["total"] == 0
    file = upload(client, path, name="成功导入").json()["file"]
    file_path = path + "/" + str(file["id"])
    with monkeypatch.context() as patch:
        patch.setattr(Projects, "save_upload", fail_after_insert)
        check(upload(client, file_path + "/versions", "updated.yaml"), "Error", 500)
    assert client.get(file_path).json() == file
    assert client.get(file_path + "/versions").json()["total"] == 1
    assert client.get(file_path + "/interfaces").json()["total"] == 2


def test_different_yaml_files_bind_operations_to_their_own_modules(tmp_path, monkeypatch, client):
    from types import SimpleNamespace

    from app.api.registry import CONTRACT_IMPLEMENTATIONS

    def first(repository, path, query, body, upload):
        return {"implementation": "alpha"}

    def second(repository, path, query, body, upload):
        return {"implementation": "beta"}

    monkeypatch.setattr(
        "app.api.business.execute",
        lambda handler, path, query, body, upload: handler(None, path, query, body, upload),
    )
    specification = dict(SPEC)
    specification["paths"] = {"/api/v1/projects": {"get": SPEC["paths"]["/api/v1/projects"]["get"]}}
    for name, handler in [("rest-alpha", first), ("rest-beta", second)]:
        monkeypatch.setitem(
            CONTRACT_IMPLEMENTATIONS,
            name,
            SimpleNamespace(
                OPERATIONS={"listProjects": handler},
                SYSTEM_OPERATIONS=set(),
            ),
        )
        file = tmp_path / (name + ".yaml")
        file.write_text(yaml.safe_dump(specification))
        with TestClient(create_app(str(file))) as selected:
            response = selected.get("/api/v1/projects")
            assert response.status_code == 200
            assert response.json() == {"implementation": name.removeprefix("rest-")}


def test_rest_project_contract_parses_multipart_file_fields():
    content = (ROOT / "docs/rest-project.yaml").read_bytes()
    parsed = parse_upload("rest-project.yaml", content)
    assert len(parsed["interfaces"]) == 20
    upload_operation = next(
        i for i in parsed["interfaces"] if i["operation_id"] == "importYamlFile"
    )
    body = upload_operation["request_body"]
    assert body["content_type"] == "multipart/form-data"
    assert body["schema"]["required"] == ["name", "file"]
    assert body["schema"]["properties"]["file"]["format"] == "binary"
    assert body["schema"]["properties"]["name"]["maxLength"] == 120


def test_multipart_components_preserve_encoding_and_sensitive_examples():
    document = yaml.safe_load((EXAMPLES / "valid.yaml").read_text())
    document["components"]["requestBodies"] = {
        "Upload": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "token": {"type": "string", "example": "PRIVATE_TOKEN"},
                        },
                    },
                    "encoding": {"file": {"contentType": "application/octet-stream"}},
                }
            },
        }
    }
    document["paths"]["/orders"]["post"]["requestBody"] = {
        "$ref": "#/components/requestBodies/Upload"
    }
    parsed = parse_upload("upload.yml", yaml.safe_dump(document).encode())
    body = next(i for i in parsed["interfaces"] if i["method"] == "POST")["request_body"]
    assert body["required"]
    assert body["encoding"]["file"]["contentType"] == "application/octet-stream"
    assert "PRIVATE_TOKEN" not in str(redact(body))
    Draft4Validator(SPEC).evolve(
        schema={"$ref": "#/components/schemas/RequestBodyDefinition"}
    ).validate(redact(body))


def test_mixed_media_types_are_rejected_without_discarding_a_definition():
    document = yaml.safe_load((EXAMPLES / "valid.yaml").read_text())
    document["paths"]["/orders"]["post"]["requestBody"]["content"]["multipart/form-data"] = {
        "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}
    }
    with pytest.raises(BusinessError) as exc:
        parse_upload("mixed.yaml", yaml.safe_dump(document).encode())
    assert exc.value.payload["issues"][0]["location"].endswith("/requestBody/content")


def test_rest_project_import_details_and_version_update(client, isolated_database):
    pid = check(client.post("/api/v1/projects", json={"name": "导入完整契约"}), "Project", 201)[
        "id"
    ]
    path = f"/api/v1/projects/{pid}/yaml-files"
    content = (ROOT / "docs/rest-project.yaml").read_bytes()
    imported = check(
        client.post(
            path,
            data={"name": "rest-project"},
            files={"file": ("rest-project.yaml", content, "application/yaml")},
        ),
        "ImportResult",
        201,
    )
    assert imported["version"]["interface_count"] == 20
    file = path + "/" + str(imported["file"]["id"])
    interfaces = check(client.get(file + "/interfaces"), "InterfacePage")["items"]
    for interface in interfaces:
        detail = check(client.get(file + "/interfaces/" + str(interface["id"])), "InterfaceDetail")
        if detail["operation_id"] in {"importYamlFile", "updateYamlFile"}:
            body = detail["request_body"]
            assert body["content_type"] == "multipart/form-data"
            assert body["schema"]["properties"]["file"]["format"] == "binary"
    updated = check(
        client.post(
            file + "/versions", files={"file": ("rest-project.yaml", content, "application/yaml")}
        ),
        "ImportResult",
        201,
    )
    assert updated["version"]["version"] == 2
    assert updated["version"]["interface_count"] == 20
    assert check(client.get(file + "/interfaces?version=1"), "InterfacePage")["items"] == interfaces
    saved = check(client.get(file + "/versions/1/content"), "VersionContent")
    assert yaml.safe_load(saved["content"])["paths"]["/api/v1/projects/{project_id}/yaml-files"][
        "post"
    ]["requestBody"]["content"]["multipart/form-data"]


def test_upload_failure_logs_code_location_operation_and_request_id(client):
    import json

    from app.config.settings import absolute, load_settings

    response = upload(client, "/api/v1/projects/1/yaml-files", "invalid.yaml", name="日志测试")
    assert response.status_code == 400
    directory = absolute(load_settings()["logging"]["directory"])
    records = [json.loads(line) for line in (directory / "error.log").read_text().splitlines()]
    record = next(r for r in records if r["request_id"] == response.headers["X-Request-ID"])
    assert record["operation"] == "importYamlFile"
    assert record["route"] == "/api/v1/projects/{project_id}/yaml-files"
    assert record["code"] == "UNSUPPORTED_SCHEMA"
    assert record["status"] == 400
    assert record["issues"] == response.json()["issues"]
    assert record["detail"] == response.json()["detail"]


def test_internal_failure_logs_stack_without_exception_secrets(client, monkeypatch):
    import json

    from app.config.settings import absolute, load_settings

    def failing(*args):
        raise RuntimeError("PRIVATE_PASSWORD PRIVATE_TOKEN")

    monkeypatch.setattr("app.api.business.execute", failing)
    response = client.get(
        "/api/v1/projects?name=PRIVATE_QUERY", headers={"Authorization": "PRIVATE_AUTH"}
    )
    assert response.status_code == 500
    directory = absolute(load_settings()["logging"]["directory"])
    text = (directory / "error.log").read_text()
    record = next(
        json.loads(line)
        for line in text.splitlines()
        if json.loads(line)["request_id"] == response.headers["X-Request-ID"]
    )
    assert record["operation"] == "listProjects"
    assert record["exception"] == "RuntimeError"
    assert any(frame["function"] == "failing" and frame["line"] > 0 for frame in record["stack"])
    assert "PRIVATE_" not in text
