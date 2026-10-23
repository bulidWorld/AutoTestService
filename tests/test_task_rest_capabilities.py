"""REST writes persist extended policies and execute from immutable DB snapshots."""

import copy
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_task_business as fixtures
import yaml
from test_task_business import schema_check, wait_run

api, database = fixtures.api, fixtures.database


@pytest.fixture
def status_server():
    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["calls"] += 1
            body = json.dumps({"ready": state["calls"] % 3 == 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{http.server_port}", "state": state}
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def task_payload(api, status_server, tmp_path):
    response = api.post("/api/v1/projects", json={"name": "策略测试"})
    assert response.status_code == 201, response.text
    project = response.json()
    base = f"/api/v1/projects/{project['id']}"
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "status", "version": "1"},
        "paths": {"/status": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    imported = api.post(
        base + "/yaml-files",
        data={"name": "status"},
        files={"file": ("status.yaml", yaml.safe_dump(doc).encode(), "application/yaml")},
    )
    assert imported.status_code == 201, imported.text
    fid = imported.json()["file"]["id"]
    interface = api.get(base + f"/yaml-files/{fid}/interfaces").json()["items"][0]
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER, label TEXT)")
    payload = {
        "name": "三项能力",
        "base_url": status_server["url"],
        "global_parameters": [
            {"name": "id", "source_type": "fixed", "value_type": "integer", "value": 42},
            {
                "name": "label",
                "source_type": "expression",
                "value_type": "string",
                "expression_source": {"step_index": 0},
            },
            {
                "name": "ready",
                "source_type": "rest",
                "value_type": "boolean",
                "rest_source": {"step_index": 2, "extract_expression": "ready"},
            },
        ],
        "steps": [
            {
                "step_type": "compute",
                "execution_scope": "main",
                "position": 10,
                "options": {
                    "expressions": [
                        {"name": "label", "expression": 'concat("order-", str(param("id")))'}
                    ]
                },
            },
            {
                "step_type": "sql",
                "execution_scope": "main",
                "position": 20,
                "options": {
                    "sql": {
                        "driver": "sqlite",
                        "datasource": str(path),
                        "statement": "INSERT INTO orders VALUES (?, ?)",
                        "parameters": ["id", "label"],
                    }
                },
            },
            {
                "step_type": "rest",
                "execution_scope": "main",
                "position": 30,
                "file_id": fid,
                "interface_id": interface["id"],
                "version": 1,
                "bindings": [],
                "headers": [],
                "assertion_code": 'def assert_response(responseBody, globalParam, statusCode, headers):\n    return statusCode == 200 and responseBody["ready"]',
                "options": {
                    "poll": {"max_attempts": 4, "interval_milliseconds": 1, "timeout_seconds": 5}
                },
            },
        ],
    }
    return base, payload, path


def test_rest_create_update_read_copy_and_execute(api, database, status_server, tmp_path):
    base, payload, path = task_payload(api, status_server, tmp_path)
    created = schema_check(api.post(base + "/tasks", json=payload), "Task", 201)
    task = base + f"/tasks/{created['id']}"
    read = schema_check(api.get(task), "Task")
    assert [step["options"] for step in read["steps"]] == [s["options"] for s in payload["steps"]]
    assert read["global_parameters"][1]["expression_source"] == {"step_id": read["steps"][0]["id"]}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM orders").fetchall() == []
    assert status_server["state"]["calls"] == 0  # Saving never sends or executes SQL.

    run = api.post(task + "/runs", json={})
    assert run.status_code == 202, run.text
    rid = run.json()["id"]
    result = wait_run(api, task, rid)
    assert result["run"]["status"] == "succeeded", result
    assert status_server["state"]["calls"] == 3
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM orders").fetchall() == [(42, "order-42")]

    updated_payload = copy.deepcopy(payload)
    updated_payload["global_parameters"][0]["value"] = 43
    updated_payload["steps"][0]["options"]["expressions"][0]["expression"] = (
        'concat("updated-", str(param("id")))'
    )
    updated_payload["steps"][1]["options"]["sql"]["statement"] = (
        "INSERT INTO orders (label, id) VALUES (?, ?)"
    )
    updated_payload["steps"][1]["options"]["sql"]["parameters"] = ["label", "id"]
    updated_payload["steps"][2]["options"]["poll"]["max_attempts"] = 5
    for old, new in zip(created["steps"], updated_payload["steps"]):
        new["id"] = old["id"]
    # Updates accept stable source IDs as well as temporary array indices.
    updated_payload["global_parameters"][1]["expression_source"] = created["global_parameters"][1][
        "expression_source"
    ]
    updated_payload["global_parameters"][2]["rest_source"] = created["global_parameters"][2][
        "rest_source"
    ]
    updated = schema_check(api.put(task, json=updated_payload), "Task")
    assert [s["id"] for s in updated["steps"]] == [s["id"] for s in created["steps"]]
    assert [s["options"] for s in updated["steps"]] == [
        s["options"] for s in updated_payload["steps"]
    ]
    assert schema_check(api.get(task), "Task")["steps"] == updated["steps"]
    with database() as db:
        saved = db.execute(
            "SELECT options FROM task_steps WHERE task_id=%s ORDER BY position", (created["id"],)
        ).fetchall()
        assert [row[0] for row in saved] == [s["options"] for s in updated_payload["steps"]]
        snapshot = db.execute(
            "SELECT config_snapshot FROM task_runs WHERE id=%s", (rid,)
        ).fetchone()[0]
        assert snapshot["steps"][0]["options"] == payload["steps"][0]["options"]
        assert snapshot["steps"][2]["options"]["poll"]["max_attempts"] == 4
    rerun = api.post(task + "/runs", json={})
    assert rerun.status_code == 202, rerun.text
    assert wait_run(api, task, rerun.json()["id"])["run"]["status"] == "succeeded"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM orders ORDER BY id").fetchall() == [
            (42, "order-42"),
            (43, "updated-43"),
        ]
    copied = schema_check(api.post(task + "/copy", json={"name": "策略副本"}), "Task", 201)
    assert [s["options"] for s in copied["steps"]] == [s["options"] for s in updated["steps"]]
    assert copied["global_parameters"][1]["expression_source"] == {
        "step_id": copied["steps"][0]["id"]
    }
    assert copied["steps"][0]["id"] != updated["steps"][0]["id"]


@pytest.mark.parametrize(
    "change",
    ["unknown_sql_parameter", "future_sql_parameter", "invalid_poll", "invalid_expression"],
)
def test_invalid_rest_update_rolls_back(api, status_server, tmp_path, change):
    base, payload, path = task_payload(api, status_server, tmp_path)
    created = schema_check(api.post(base + "/tasks", json=payload), "Task", 201)
    task = base + f"/tasks/{created['id']}"
    modified = copy.deepcopy(payload)
    modified["name"] = "不应保存"
    if change == "unknown_sql_parameter":
        modified["steps"][1]["options"]["sql"]["parameters"] = ["missing", "label"]
    elif change == "future_sql_parameter":
        modified["steps"][1]["position"] = 5  # label is produced by the later compute.
        modified["steps"][1]["options"]["sql"]["parameters"] = ["id", "label"]
    elif change == "invalid_poll":
        modified["steps"][2]["options"]["poll"]["max_attempts"] = 0
    else:
        modified["steps"][0]["options"]["expressions"][0]["expression"] = "open('/etc/passwd')"
    rejected = api.put(task, json=modified)
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["issues"]
    unchanged = schema_check(api.get(task), "Task")
    assert unchanged["name"] == created["name"]
    assert unchanged["steps"] == created["steps"]
    assert status_server["state"]["calls"] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM orders").fetchall() == []
