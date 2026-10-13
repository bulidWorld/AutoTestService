"""Task contract and real PostgreSQL/HTTP lifecycle verification in an isolated schema."""

import copy
import importlib.util
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_schema_validator import OAS30Validator
from openapi_spec_validator import validate

from app.config.settings import ROOT
from app.main import create_app
from app.repository.database import connect
from app.service.errors import BusinessError
from app.service.task_config import build_request, extract, validate_config

SPEC = yaml.safe_load((ROOT / "docs/rest-task.yaml").read_text())


def schema_check(response, name, status=200):
    assert response.status_code == status, response.text
    from jsonschema import RefResolver

    OAS30Validator(
        SPEC["components"]["schemas"][name], resolver=RefResolver.from_schema(SPEC)
    ).validate(response.json())
    return response.json()


def bare_config():
    return {
        "base_url": "http://localhost/prefix",
        "global_parameters": [],
        "steps": [
            {
                "id": 1,
                "bindings": [],
                "interface": {
                    "method": "GET",
                    "path": "/ok",
                    "parameters": [],
                    "request_body": None,
                },
                "document": {},
            }
        ],
    }


def test_extraction_preserves_types_and_rejects_scripts():
    assert extract({"data": {"a": 1}}, "data.a", "integer", "test") == 1
    assert (
        extract({"data": {"items": [{"id": True}]}}, "data.items[0].id", "boolean", "test") is True
    )
    for expr in ['__import__("os")', "data.*", "data[-1]", "data..a"]:
        with pytest.raises(BusinessError):
            extract({}, expr, "integer", "test")
    for data, code in [
        ({"data": {}}, "EXTRACTION_NOT_FOUND"),
        ({"data": {"a": True}}, "EXTRACTION_TYPE_ERROR"),
        ({"data": {"a": None}}, "EXTRACTION_TYPE_ERROR"),
    ]:
        with pytest.raises(BusinessError) as error:
            extract(data, "data.a", "integer", "test")
        assert error.value.payload["code"] == code


def test_required_bindings_encoding_and_json_structure():
    config = bare_config()
    step = config["steps"][0]
    step["interface"].update(
        path="/orders/{id}",
        parameters=[
            {"name": "id", "location": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "id",
                "location": "query",
                "required": False,
                "schema": {"type": "array", "items": {"type": "integer"}},
            },
        ],
        request_body={
            "content_type": "application/json",
            "required": True,
            "schema": {
                "type": "object",
                "required": ["data"],
                "properties": {
                    "data": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"a": {"type": "integer"}},
                            "required": ["a"],
                        },
                    }
                },
            },
        },
    )
    step["bindings"] = [
        {"location": "path", "name": "id", "binding": {"kind": "literal", "value": "a /中?"}},
        {"location": "query", "name": "id", "binding": {"kind": "literal", "value": [1, 2]}},
        {"location": "body", "name": "/data", "binding": {"kind": "literal", "value": [{"a": 1}]}},
    ]
    rendered = build_request(config, step, values={})
    assert rendered["actual_url"] == "http://localhost/prefix/orders/a%20%2F%E4%B8%AD%3F?id=1&id=2"
    assert json.loads(rendered["request_body"]) == {"data": [{"a": 1}]}
    step["bindings"].pop()
    with pytest.raises(BusinessError):
        validate_config(config)


def test_static_dependency_rejection_sends_no_requests():
    config = bare_config()
    config["global_parameters"] = [
        {
            "name": "id",
            "value_type": "integer",
            "source_type": "rest",
            "rest_source": {"step_id": 1, "extract_expression": "data.a"},
        }
    ]
    config["steps"][0]["interface"]["parameters"] = [
        {"name": "id", "location": "query", "required": True, "schema": {"type": "integer"}}
    ]
    config["steps"][0]["bindings"] = [
        {"location": "query", "name": "id", "binding": {"kind": "global", "parameter_name": "id"}}
    ]
    with pytest.raises(BusinessError):
        validate_config(config)


def test_both_contracts_registered_with_valid_references(monkeypatch):
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda _: None)
    with TestClient(create_app()) as client:
        doc = client.get("/openapi.json").json()
        validate(doc)
        assert "/api/v1/projects" in doc["paths"]
        assert "/api/v1/projects/{project_id}/tasks" in doc["paths"]
        assert (
            client.post(
                "/api/v1/projects/1/tasks",
                json={
                    "name": "x",
                    "base_url": "http://localhost",
                    "global_parameters": [],
                    "steps": [],
                    "unknown": 1,
                },
            ).status_code
            == 400
        )


@pytest.fixture
def server():
    calls = []
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append({"path": self.path, "headers": dict(self.headers)})
            status = 200
            body = {"data": {"a": len(calls), "b": "PRIVATE_TOKEN"}}
            if "/slow" in self.path:
                entered.set()
                release.wait(3)
            if "/fail" in self.path:
                status = 500
            if "/redirect" in self.path:
                status = 302
            content = b"not-json" if "/not-json" in self.path else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "PRIVATE_COOKIE")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append(
                {"path": self.path, "headers": dict(self.headers), "body": json.loads(content)}
            )
            self.send_response(204)
            self.end_headers()

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield {
        "url": f"http://127.0.0.1:{http.server_port}/prefix",
        "calls": calls,
        "entered": entered,
        "release": release,
    }
    release.set()
    http.shutdown()
    http.server_close()
    thread.join()


@pytest.fixture
def database(monkeypatch, tmp_path):
    if os.environ.get("AUTOTEST_DB_TESTS") != "1":
        pytest.skip("Set AUTOTEST_DB_TESTS=1 for isolated PostgreSQL integration tests")
    name = "task_test_" + uuid4().hex
    identifier = psycopg.sql.Identifier(name)
    with connect() as db:
        db.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(identifier))
        db.execute(psycopg.sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(identifier))
        spec = importlib.util.spec_from_file_location(
            "task_schema", ROOT / "scripts/sync_schema.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for _, sql in module.load_statements(ROOT / "db"):
            db.execute(sql)

    def isolated():
        db = connect()
        db.execute(psycopg.sql.SQL("SET search_path TO {},pg_catalog").format(identifier))
        db.commit()
        return db

    monkeypatch.setattr("app.repository.projects.connect", isolated)
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump({"logging": {"directory": str(tmp_path / "logs")}}))
    monkeypatch.setenv("APP_CONFIG", str(config))
    yield isolated
    with connect() as db:
        db.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))


@pytest.fixture
def api(database):
    with TestClient(create_app()) as client:
        yield client


def asset(api, server):
    project = api.post("/api/v1/projects", json={"name": "任务测试"}).json()
    pid = project["id"]
    p = f"/api/v1/projects/{pid}"
    doc = {"openapi": "3.0.3", "info": {"title": "mock", "version": "1"}, "paths": {}}
    for path in ["/init", "/ok", "/fail", "/slow", "/not-json", "/redirect"]:
        doc["paths"][path] = {"get": {"responses": {"200": {"description": "ok"}}}}
    doc["paths"]["/orders/{id}"] = {
        "post": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {
                    "name": "id",
                    "in": "query",
                    "schema": {"type": "array", "items": {"type": "integer"}},
                },
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["data"],
                            "properties": {"data": {"type": "object"}},
                        }
                    }
                },
            },
            "responses": {"204": {"description": "ok"}},
        }
    }
    imported = api.post(
        p + "/yaml-files",
        data={"name": "mock"},
        files={"file": ("mock.yaml", yaml.safe_dump(doc).encode(), "application/yaml")},
    )
    assert imported.status_code == 201, imported.text
    fid = imported.json()["file"]["id"]
    interfaces = api.get(p + f"/yaml-files/{fid}/interfaces").json()["items"]
    steps = {
        i["path"]: {"interface_id": i["id"], "file_id": fid, "version": 1, "bindings": []}
        for i in interfaces
    }

    def create(names):
        payload = {
            "name": "task-" + uuid4().hex,
            "base_url": server["url"],
            "global_parameters": [],
            "steps": [copy.deepcopy(steps[n]) for n in names],
        }
        created = schema_check(api.post(p + "/tasks", json=payload), "Task", 201)
        return p + "/tasks/" + str(created["id"]), created

    return p, fid, steps, create


def wait_run(api, t, rid):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        data = schema_check(api.get(t + f"/runs/{rid}"), "RunDetail")
        if data["run"]["status"] in ["succeeded", "failed"]:
            return data
        time.sleep(0.03)
    pytest.fail("run did not finish")


def test_dynamic_shared_step_typed_request_and_history(api, server, database):
    p, fid, steps, create = asset(api, server)
    t, initial = create(["/init"])
    sid = initial["steps"][0]["id"]
    payload = {
        "name": initial["name"],
        "base_url": server["url"],
        "global_parameters": [
            {
                "name": "order_id",
                "source_type": "rest",
                "value_type": "integer",
                "rest_source": {"step_id": sid, "extract_expression": "data.a"},
            },
            {
                "name": "token",
                "source_type": "rest",
                "value_type": "string",
                "sensitive": True,
                "rest_source": {"step_id": sid, "extract_expression": "data.b"},
            },
            {"name": "data", "source_type": "fixed", "value_type": "object", "value": {"a": 1}},
            {"name": "ids", "source_type": "fixed", "value_type": "array", "value": [1, 2]},
        ],
        "steps": [
            dict(steps["/init"], id=sid),
            dict(
                steps["/orders/{id}"],
                bindings=[
                    {
                        "location": "path",
                        "name": "id",
                        "binding": {"kind": "global", "parameter_name": "order_id"},
                    },
                    {
                        "location": "query",
                        "name": "id",
                        "binding": {"kind": "global", "parameter_name": "ids"},
                    },
                    {
                        "location": "header",
                        "name": "Authorization",
                        "binding": {"kind": "global", "parameter_name": "token"},
                    },
                    {
                        "location": "body",
                        "name": "/data",
                        "binding": {"kind": "global", "parameter_name": "data"},
                    },
                ],
            ),
        ],
    }
    updated = schema_check(api.put(t, json=payload), "Task")
    assert updated["steps"][0]["execution_phase"] == "parameters"
    preview = schema_check(
        api.post(
            t + "/parameters/preview",
            json={
                "base_url": server["url"],
                "global_parameters": payload["global_parameters"],
                "steps": [dict(s) for s in payload["steps"] if "id" in s],
                "parameter_name": "order_id",
            },
        ),
        "PreviewParameterResult",
    )
    assert preview["value"] == 1
    r = schema_check(api.post(t + "/runs"), "Run", 202)
    result = wait_run(api, t, r["id"])
    assert result["run"]["status"] == "succeeded"
    assert len(server["calls"]) == 3
    assert server["calls"][-1]["path"] == "/prefix/orders/2?id=1&id=2"
    assert server["calls"][-1]["body"] == {"data": {"a": 1}}
    assert server["calls"][-1]["headers"]["Authorization"] == "PRIVATE_TOKEN"
    requests = schema_check(api.get(t + f"/runs/{r['id']}/requests"), "RequestPage")
    assert [q["phase"] for q in requests["items"]] == ["parameters", "steps"]
    assert "PRIVATE_" not in json.dumps(requests) and "PRIVATE_" not in json.dumps(result)
    assert (
        result["global_parameters"][0]["request_id"] == result["global_parameters"][1]["request_id"]
    )
    copied = schema_check(api.post(t + "/copy", json={"name": "copy"}), "Task", 201)
    assert (
        copied["global_parameters"][0]["rest_source"]["step_id"] == copied["steps"][0]["id"] != sid
    )
    assert api.delete(p + f"/yaml-files/{fid}?confirm=true").status_code == 409
    assert api.get(p + f"/yaml-files/{fid}/deletion-preview").json()["can_delete"] is False
    bad = copy.deepcopy(payload)
    bad["steps"] = bad["steps"][1:]
    assert api.put(t, json=bad).status_code == 400
    r2 = schema_check(api.post(t + "/runs"), "Run", 202)
    second = wait_run(api, t, r2["id"])
    assert second["global_parameters"][0]["value"] != result["global_parameters"][0]["value"]
    schema_check(api.delete(t + "?confirm=true"), "DeletionResult")
    with database() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM task_runs WHERE task_id=%s", (r["task_id"],)
            ).fetchone()[0]
            == 0
        )
    assert api.delete(p + "?confirm=true").status_code == 200


@pytest.mark.parametrize(
    "failure,code",
    [
        ("/fail", "HTTP_STATUS_ERROR"),
        ("/redirect", "HTTP_STATUS_ERROR"),
        ("/not-json", "RESPONSE_NOT_JSON"),
    ],
)
def test_initialization_failure_skips_unexecuted_work(api, server, failure, code):
    _, _, steps, create = asset(api, server)
    t, created = create([failure, "/ok"])
    sid = created["steps"][0]["id"]
    body = {
        "name": created["name"],
        "base_url": server["url"],
        "global_parameters": [
            {
                "name": "x",
                "source_type": "rest",
                "value_type": "integer",
                "rest_source": {"step_id": sid, "extract_expression": "data.a"},
            }
        ],
        "steps": [dict(steps[failure], id=sid), dict(steps["/ok"], id=created["steps"][1]["id"])],
    }
    assert api.put(t, json=body).status_code == 200
    rid = api.post(t + "/runs").json()["id"]
    result = wait_run(api, t, rid)
    assert result["run"]["error_code"] == code
    assert [s["status"] for s in result["steps"]] == ["failed", "skipped"]
    assert len(server["calls"]) == 1


def test_concurrency_snapshot_edit_and_delete_restrictions(api, server):
    p, _, steps, create = asset(api, server)
    t, created = create(["/slow", "/ok"])
    other, _ = create(["/ok"])
    r = schema_check(api.post(t + "/runs"), "Run", 202)
    assert server["entered"].wait(3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.post(t + "/runs"), range(2)))
    assert all(v.status_code == 409 for v in responses)
    assert api.delete(t + "?confirm=true").status_code == 409
    assert api.delete(p + "?confirm=true").status_code == 409
    other_run = api.post(other + "/runs").json()["id"]
    assert wait_run(api, other, other_run)["run"]["status"] == "succeeded"
    changed = {
        "name": created["name"],
        "base_url": server["url"],
        "global_parameters": [],
        "steps": [dict(steps["/fail"], id=created["steps"][1]["id"])],
    }
    assert api.put(t, json=changed).status_code == 200
    server["release"].set()
    result = wait_run(api, t, r["id"])
    assert result["run"]["status"] == "succeeded"
    assert [s["path"] for s in result["config_snapshot"]["task"]["steps"]] == ["/slow", "/ok"]
    assert server["calls"][-1]["path"] == "/prefix/ok"


def test_restart_marks_incomplete_runs_without_resending(database, server):
    from app.service.task_execution import Dispatcher

    with database() as db:
        pid = db.execute("INSERT INTO projects(name) VALUES('recovery') RETURNING id").fetchone()[0]
        tid = db.execute(
            "INSERT INTO tasks(project_id,name,base_url) VALUES(%s,'old',%s) RETURNING id",
            (pid, server["url"]),
        ).fetchone()[0]
        rid = db.execute(
            "INSERT INTO task_runs(task_id,project_id,status,config_snapshot) VALUES(%s,%s,'running','{}') RETURNING id",
            (tid, pid),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO task_run_steps(run_id,step_id,position,status) VALUES(%s,1,1,'running')",
            (rid,),
        )
    dispatcher = Dispatcher()
    dispatcher.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with database() as db:
                row = db.execute(
                    "SELECT status,error_code FROM task_runs WHERE id=%s", (rid,)
                ).fetchone()
                if row[0] == "failed":
                    break
            time.sleep(0.03)
        assert row == ("failed", "EXECUTION_INTERRUPTED")
        assert server["calls"] == []
    finally:
        dispatcher.close()


def test_step_failure_stops_pipeline_and_records_before_send(api, server, database):
    _, _, _, create = asset(api, server)
    t, _ = create(["/slow", "/fail", "/ok"])
    rid = api.post(t + "/runs").json()["id"]
    assert server["entered"].wait(3)
    with database() as db:
        row = db.execute(
            "SELECT actual_url,finished_at FROM task_requests WHERE run_id=%s", (rid,)
        ).fetchone()
        assert row[0].endswith("/slow") and row[1] is None
    server["release"].set()
    result = wait_run(api, t, rid)
    assert result["run"]["error_code"] == "HTTP_STATUS_ERROR"
    assert [s["status"] for s in result["steps"]] == ["succeeded", "failed", "skipped"]
    assert [c["path"] for c in server["calls"]] == ["/prefix/slow", "/prefix/fail"]
    schema_check(api.get(t + f"/runs/{rid}/requests"), "RequestPage")


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_timeout_and_connection_errors_are_distinct(api, server, failure):
    import socket

    _, _, steps, create = asset(api, server)
    t, created = create(["/slow" if failure == "timeout" else "/ok", "/ok"])
    base_url = server["url"]
    if failure == "connection":
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            unused_port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{unused_port}"
    body = {
        "name": created["name"],
        "base_url": base_url,
        "timeout_seconds": 1,
        "global_parameters": [],
        "steps": [
            dict(steps["/slow" if failure == "timeout" else "/ok"], id=created["steps"][0]["id"]),
            dict(steps["/ok"], id=created["steps"][1]["id"]),
        ],
    }
    assert api.put(t, json=body).status_code == 200
    rid = api.post(t + "/runs").json()["id"]
    result = wait_run(api, t, rid)
    assert result["run"]["error_code"] == (
        "TIMEOUT" if failure == "timeout" else "CONNECTION_ERROR"
    )
    assert [s["status"] for s in result["steps"]] == ["failed", "skipped"]
    assert len(server["calls"]) == (1 if failure == "timeout" else 0)


@pytest.mark.parametrize(
    "expr,value_type,code",
    [
        ("data.missing", "integer", "EXTRACTION_NOT_FOUND"),
        ("data.a", "string", "EXTRACTION_TYPE_ERROR"),
    ],
)
def test_sensitive_failed_extraction_does_not_expose_response(api, server, expr, value_type, code):
    _, _, steps, create = asset(api, server)
    t, created = create(["/init", "/ok"])
    sid = created["steps"][0]["id"]
    body = {
        "name": created["name"],
        "base_url": server["url"],
        "global_parameters": [
            {
                "name": "secret",
                "source_type": "rest",
                "value_type": value_type,
                "sensitive": True,
                "rest_source": {"step_id": sid, "extract_expression": expr},
            }
        ],
        "steps": [dict(steps["/init"], id=sid), dict(steps["/ok"], id=created["steps"][1]["id"])],
    }
    assert api.put(t, json=body).status_code == 200
    rid = api.post(t + "/runs").json()["id"]
    result = wait_run(api, t, rid)
    assert result["run"]["error_code"] == code
    assert [s["status"] for s in result["steps"]] == ["failed", "skipped"]
    qid = result["global_parameters"][0]["request_id"]
    assert qid is not None
    request = schema_check(api.get(t + f"/runs/{rid}/requests/{qid}"), "Request")
    assert request["response_body"] == "***" and request["redacted"]
    assert "PRIVATE_" not in json.dumps(request)


def test_sensitive_fixed_edit_preserves_value_and_validates_ownership(api, server, database):
    p, _, steps, create = asset(api, server)
    t, created = create(["/ok"])
    base = {
        "name": created["name"],
        "base_url": server["url"],
        "steps": [dict(steps["/ok"], id=created["steps"][0]["id"])],
        "global_parameters": [
            {
                "name": "credential",
                "source_type": "fixed",
                "value_type": "string",
                "value": "PRIVATE_FIXED",
                "sensitive": True,
            }
        ],
    }
    safe = schema_check(api.put(t, json=base), "Task")
    assert "value" not in safe["global_parameters"][0] and safe["global_parameters"][0]["redacted"]
    del base["global_parameters"][0]["value"]
    assert api.put(t, json=base).status_code == 200
    with database() as db:
        assert (
            db.execute(
                "SELECT value FROM task_parameters WHERE task_id=%s", (created["id"],)
            ).fetchone()[0]
            == "PRIVATE_FIXED"
        )
    other, _ = create(["/ok"])
    wrong = copy.deepcopy(base)
    wrong["steps"][0]["id"] = api.get(other).json()["steps"][0]["id"]
    assert api.put(t, json=wrong).status_code == 400
    assert api.get(t + "/runs/999999").status_code == 404
    schema_check(api.get(p + "/tasks?name=task"), "TaskPage")
    schema_check(api.get(t + "/deletion-preview"), "DeletionPreview")
    schema_check(api.post(t + "/validate", json={"mode": "run"}), "ValidationResult")


def test_foreign_interfaces_and_duplicate_task_names_rejected(api, server):
    p, _fid, steps, create = asset(api, server)
    _t, created = create(["/ok"])
    other_project = api.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    body = {
        "name": created["name"],
        "base_url": server["url"],
        "global_parameters": [],
        "steps": [steps["/ok"]],
    }
    assert api.post(p + "/tasks", json=body).status_code == 409
    assert api.post(f"/api/v1/projects/{other_project}/tasks", json=body).status_code == 404
    assert api.get(f"/api/v1/projects/{other_project}/tasks/{created['id']}").status_code == 404


def test_declared_sensitive_fixed_values_never_change_binding_types():
    from app.service.task_config import safe_task

    config = bare_config()
    config["global_parameters"] = [
        {
            "name": "credential",
            "value_type": "integer",
            "source_type": "fixed",
            "value": 123,
            "sensitive": True,
        }
    ]
    config["steps"][0]["bindings"] = [
        {
            "location": "header",
            "name": "Authorization",
            "binding": {"kind": "literal", "value": "PRIVATE_HEADER"},
        }
    ]
    safe = safe_task(config)
    assert "value" not in safe["global_parameters"][0]
    assert safe["steps"][0]["bindings"][0]["binding"] == {"kind": "literal", "redacted": True}


def test_sensitive_literal_edit_requires_existing_redacted_binding(api, server):
    _, _, steps, create = asset(api, server)
    t, created = create(["/ok"])
    sid = created["steps"][0]["id"]
    body = {
        "name": created["name"],
        "base_url": server["url"],
        "global_parameters": [],
        "steps": [
            dict(
                steps["/ok"],
                id=sid,
                bindings=[
                    {
                        "location": "header",
                        "name": "Authorization",
                        "binding": {"kind": "literal", "value": "PRIVATE_LITERAL"},
                    },
                    {
                        "location": "header",
                        "name": "X-Label",
                        "binding": {"kind": "literal", "value": "public"},
                    },
                ],
            )
        ],
    }
    assert api.put(t, json=body).status_code == 200
    del body["steps"][0]["bindings"][0]["binding"]["value"]
    assert api.put(t, json=body).status_code == 200
    del body["steps"][0]["bindings"][1]["binding"]["value"]
    assert api.put(t, json=body).status_code == 400


def test_nonfinite_json_body_is_rejected_without_database(monkeypatch):
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda _: None)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/projects/1/tasks",
            content='{"name":"x","base_url":"http://localhost","global_parameters":[{"name":"n","value_type":"number","value":NaN}],"steps":[]}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


@pytest.mark.parametrize(
    "url", ["http://[invalid", "http://localhost:bad", "ftp://localhost", "http://localhost/?q=1"]
)
def test_invalid_execution_addresses_have_field_errors(url):
    config = bare_config()
    config["base_url"] = url
    with pytest.raises(BusinessError) as error:
        validate_config(config)
    assert (
        error.value.status == 400
        and error.value.payload["issues"][0]["location"] == "body.base_url"
    )
