"""Real PostgreSQL and local HTTP acceptance tests for execution trees."""

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_task_business as fixtures
import yaml
from test_task_business import schema_check, schema_check_list, wait_run

from app.service.errors import BusinessError
from app.service.task_config import validate_config
from app.service.task_import import parse_upload

api, database = fixtures.api, fixtures.database


@pytest.fixture
def node_server():
    state = {"calls": [], "active": 0, "maximum": 0}
    lock = threading.Lock()
    paired = threading.Barrier(2)
    loaded = threading.Event()
    released = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                state["calls"].append((self.path, body, dict(self.headers)))
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                if state["active"] == 8:
                    loaded.set()
            try:
                if self.path in ("/left", "/right"):
                    paired.wait(timeout=5)
                if self.path == "/load":
                    assert loaded.wait(5)
                    time.sleep(0.02)
                if self.path == "/slow":
                    released.wait(5)
                if self.path == "/value":
                    time.sleep(0.05)
                status = (
                    500
                    if self.path in ("/fail", "/cleanup-fail")
                    else (400 if self.path == "/raw" else 200)
                )
                content = json.dumps(
                    {"data": {"id": 42, "secret": "SENTINEL_RESPONSE"}, "code": "INVALID_JSON"}
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            finally:
                with lock:
                    state["active"] -= 1

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state.update(url=f"http://127.0.0.1:{http.server_port}", released=released)
    yield state
    released.set()
    http.shutdown()
    http.server_close()
    thread.join()


def assets(api, server):
    project = api.post("/api/v1/projects", json={"name": "execution-nodes"}).json()
    base = f"/api/v1/projects/{project['id']}"
    paths = {
        name: {"get": {"responses": {"200": {"description": "ok"}}}}
        for name in (
            "/value",
            "/left",
            "/right",
            "/load",
            "/fail",
            "/after",
            "/cleanup",
            "/cleanup-fail",
            "/slow",
        )
    }
    paths["/raw"] = {
        "post": {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {"name": {"type": "string"}},
                        }
                    }
                },
            },
            "responses": {"400": {"description": "invalid"}},
        }
    }
    paths["/needs"] = {
        "get": {
            "parameters": [
                {"name": "id", "in": "query", "required": True, "schema": {"type": "integer"}}
            ],
            "responses": {"200": {"description": "ok"}},
        }
    }
    response = api.post(
        base + "/yaml-files",
        data={"name": "nodes"},
        files={
            "file": (
                "nodes.yaml",
                yaml.safe_dump(
                    {"openapi": "3.0.3", "info": {"title": "nodes", "version": "1"}, "paths": paths}
                ).encode(),
                "application/yaml",
            )
        },
    )
    assert response.status_code == 201, response.text
    fid = response.json()["file"]["id"]
    interfaces = api.get(base + f"/yaml-files/{fid}/interfaces").json()["items"]
    definitions = {i["path"]: i for i in interfaces}

    def rest(path, position, scope="main", parent=None, **kwargs):
        node = dict(
            step_type="rest",
            execution_scope=scope,
            position=position,
            file_id=fid,
            version=1,
            interface_id=definitions[path]["id"],
            bindings=[],
            headers=[],
            **kwargs,
        )
        if parent is not None:
            node["parent_step_index"] = parent
        return node

    def create(nodes, parameters=None, name="nodes"):
        task = schema_check(
            api.post(
                base + "/tasks",
                json={
                    "name": name,
                    "base_url": server["url"],
                    "global_parameters": parameters or [],
                    "steps": nodes,
                },
            ),
            "Task",
            201,
        )
        return base + f"/tasks/{task['id']}", task

    return rest, create


def source(name, index):
    return {
        "name": name,
        "source_type": "rest",
        "value_type": "integer",
        "rest_source": {"step_index": index, "extract_expression": "data.id"},
    }


def parallel(position=10, maximum=2):
    return {
        "step_type": "parallel",
        "execution_scope": "main",
        "position": position,
        "max_concurrency": maximum,
    }


def test_parallel_barrier_stats_tree_and_cleanup(api, database, node_server):
    rest, create = assets(api, node_server)
    url, task = create(
        [
            parallel(),
            rest("/left", 1, parent=0),
            rest("/right", 2, parent=0),
            {
                "step_type": "wait",
                "execution_scope": "main",
                "position": 20,
                "wait_milliseconds": 30,
            },
            rest("/cleanup", 1, "finally"),
        ],
        [source("left", 1), source("right", 2)],
    )
    rid = schema_check(api.post(url + "/runs"), "Run", 202)["id"]
    result = wait_run(api, url, rid)
    assert result["run"]["status"] == "succeeded"
    assert result["run"]["execution_stage"] == "finished"
    assert result["run"]["cleanup_status"] == "succeeded"
    assert node_server["maximum"] >= 2
    assert [s["step_type"] for s in result["steps"]] == ["parallel", "wait", "rest"]
    stats = result["steps"][0]["parallel_stats"]
    assert (
        stats["rest_total"]
        == stats["request_count"]
        == stats["response_count"]
        == stats["succeeded_count"]
        == 2
    )
    assert (
        stats["failed_count"]
        == stats["skipped_count"]
        == stats["pending_count"]
        == stats["running_count"]
        == 0
    )
    assert (
        0
        < stats["min_response_time_ms"]
        <= stats["avg_response_time_ms"]
        <= stats["max_response_time_ms"]
    )
    assert stats["avg_response_time_ms"] == round(stats["response_time_sum_ms"] / 2, 3)
    assert result["leaf_counts"]["total"] == 4 and result["control_counts"]["total"] == 1
    assert result["request_count"] == 3
    assert all(p["published"] and p["request_id"] is None for p in result["global_parameters"])
    requests = api.get(url + f"/runs/{rid}/requests").json()
    assert requests["total"] == 1 and requests["items"][0]["phase"] == "finally"
    with database() as db:
        children = db.execute(
            "SELECT id,duration_ms,error_message FROM task_run_steps WHERE run_id=%s AND parent_run_step_id IS NOT NULL",
            (rid,),
        ).fetchall()
        assert len(children) == 2
        assert all(c[1:] == (None, None) for c in children)
        assert (
            db.execute(
                "SELECT count(*) FROM task_requests WHERE run_step_id=ANY(%s)",
                ([r[0] for r in children],),
            ).fetchone()[0]
            == 0
        )
    copied = schema_check(api.post(url + "/copy", json={"name": "copy"}), "Task", 201)
    parent_id = next(s["id"] for s in copied["steps"] if s["step_type"] == "parallel")
    assert all(s["parent_step_id"] == parent_id for s in copied["steps"] if s["parent_step_id"])
    assert not {s["id"] for s in copied["steps"]} & {s["id"] for s in task["steps"]}


def test_failed_parallel_publishes_values_and_attempts_all_cleanup(api, node_server):
    rest, create = assets(api, node_server)
    needs = rest("/needs", 1, "finally")
    needs["bindings"] = [
        {"location": "query", "name": "id", "binding": {"kind": "global", "parameter_name": "id"}}
    ]
    url, _ = create(
        [
            parallel(),
            rest("/value", 1, parent=0),
            rest("/fail", 2, parent=0),
            rest("/after", 20),
            needs,
            rest("/cleanup", 2, "finally"),
        ],
        [source("id", 1)],
    )
    rid = api.post(url + "/runs").json()["id"]
    result = wait_run(api, url, rid)
    assert (
        result["run"]["error_code"] == result["run"]["primary_error"]["code"] == "PARALLEL_FAILED"
    )
    assert result["run"]["cleanup_status"] == "succeeded"
    assert result["steps"][0]["failed_child_step_id"] is not None
    stats = result["steps"][0]["parallel_stats"]
    assert stats["succeeded_count"] == stats["failed_count"] == 1
    assert stats["response_count"] == 2
    parameter = result["global_parameters"][0]
    assert (
        parameter["status"] == "succeeded"
        and parameter["value"] == 42
        and parameter["published"]
    )
    assert result["steps"][1]["error_code"] == "UPSTREAM_FAILED"
    assert result["steps"][2]["status"] == "succeeded"
    assert [c[0] for c in node_server["calls"]][-1] == "/cleanup"
    assert "/needs?id=42" in [c[0] for c in node_server["calls"]]
    assert all(c[0] != "/after" for c in node_server["calls"])


@pytest.mark.parametrize("in_parallel", [False, True])
def test_failed_source_assertion_still_supplies_cleanup(api, node_server, in_parallel):
    rest, create = assets(api, node_server)
    origin = rest(
        "/value", 1, parent=0 if in_parallel else None,
        assertion_code=(
            "def assert_response(responseBody, globalParam, statusCode, headers):\n"
            "    return statusCode == 400"
        ),
    )
    needs = rest("/needs", 1, "finally")
    needs["bindings"] = [{
        "location": "query", "name": "id",
        "binding": {"kind": "global", "parameter_name": "id"},
    }]
    nodes = ([parallel(maximum=1), origin, {
        "step_type": "wait", "execution_scope": "main", "position": 2,
        "parent_step_index": 0, "wait_milliseconds": 1,
    }] if in_parallel else [origin])
    nodes += [rest("/after", 20), needs]
    url, _ = create(nodes, [source("id", 1 if in_parallel else 0)])
    result = wait_run(api, url, api.post(url + "/runs").json()["id"])
    assert result["run"]["status"] == "failed"
    assert result["run"]["primary_error"]["code"] == (
        "PARALLEL_FAILED" if in_parallel else "ASSERTION_FAILED"
    )
    assert result["run"]["cleanup_status"] == "succeeded"
    assert result["global_parameters"][0]["value"] == 42
    assert result["global_parameters"][0]["published"]
    assert [c[0] for c in node_server["calls"]] == ["/value", "/needs?id=42"]


def test_unstarted_parallel_requests_are_skipped(api, node_server):
    rest, create = assets(api, node_server)
    url, _ = create(
        [
            parallel(maximum=1),
            rest("/fail", 1, parent=0),
            rest("/after", 2, parent=0),
            rest("/cleanup", 1, "finally"),
        ]
    )
    rid = api.post(url + "/runs").json()["id"]
    result = wait_run(api, url, rid)
    stats = result["steps"][0]["parallel_stats"]
    assert (
        stats["failed_count"]
        == stats["skipped_count"]
        == stats["request_count"]
        == stats["response_count"]
        == 1
    )
    assert [c[0] for c in node_server["calls"]] == ["/fail", "/cleanup"]


@pytest.mark.parametrize("text", ["", ' \n{"name":中文 {token} ${token} '])
def test_raw_body_exact_utf8_roundtrip_and_business_assertion(api, node_server, text):
    rest, create = assets(api, node_server)
    node = rest(
        "/raw",
        1,
        raw_body={"text": text},
        assertion_code='def assert_response(responseBody, globalParam, statusCode, headers):\n    return statusCode == 400 and responseBody["code"] == "INVALID_JSON"',
    )
    url, _ = create([node])
    copied = schema_check(api.post(url + "/copy", json={"name": "copy"}), "Task", 201)
    assert copied["steps"][0]["raw_body"]["text"] == text
    rid = api.post(url + "/runs").json()["id"]
    result = wait_run(api, url, rid)
    assert result["run"]["status"] == "succeeded"
    assert node_server["calls"][0][1] == text.encode("utf-8")
    assert node_server["calls"][0][2]["Content-Type"] == "application/json"
    requests = api.get(url + f"/runs/{rid}/requests").json()["items"]
    assert requests[0]["request_body"] == text
    assert result["config_snapshot"]["task"]["steps"][0]["raw_body"]["text"] == text


def test_global_http_limit_across_task_parallel_nodes(api, node_server):
    rest, create = assets(api, node_server)
    tasks = []
    for index in range(2):
        nodes = [parallel(maximum=8)] + [rest("/load", i, parent=0) for i in range(1, 9)]
        url, _ = create(nodes, name=f"load-{index}")
        tasks.append((url, api.post(url + "/runs").json()["id"]))
    for url, rid in tasks:
        result = wait_run(api, url, rid)
        assert result["run"]["status"] == "succeeded"
        assert result["request_count"] == 8
        assert api.get(url + f"/runs/{rid}/requests").json()["total"] == 0
    assert node_server["maximum"] == 8


@pytest.mark.parametrize("main_path", ["/value", "/fail"])
def test_cleanup_errors_do_not_overwrite_primary_error(api, node_server, main_path):
    rest, create = assets(api, node_server)
    url, _ = create(
        [rest(main_path, 1), rest("/cleanup-fail", 1, "finally"), rest("/cleanup", 2, "finally")]
    )
    result = wait_run(api, url, api.post(url + "/runs").json()["id"])
    assert result["run"]["status"] == "failed" and result["run"]["cleanup_status"] == "failed"
    assert result["run"]["error_code"] == (
        "CLEANUP_FAILED" if main_path == "/value" else "HTTP_STATUS_ERROR"
    )
    assert bool(result["run"]["primary_error"]) == (main_path == "/fail")
    assert [c[0] for c in node_server["calls"]] == [main_path, "/cleanup-fail", "/cleanup"]


def test_preview_runs_required_parallel_barrier_but_no_cleanup(api, node_server):
    rest, create = assets(api, node_server)
    url, task = create(
        [
            parallel(),
            rest("/left", 1, parent=0),
            rest("/right", 2, parent=0),
            rest("/cleanup", 1, "finally"),
        ],
        [source("target", 1)],
    )
    allowed = {
        "id",
        "step_type",
        "execution_scope",
        "position",
        "parent_step_id",
        "max_concurrency",
        "interface_id",
        "file_id",
        "version",
        "headers",
        "bindings",
        "assertion_code",
    }
    parameters = [
        {
            k: v
            for k, v in p.items()
            if k in {"name", "source_type", "value_type", "rest_source", "sensitive"}
        }
        for p in task["global_parameters"]
    ]
    response = api.post(
        url + "/parameters/preview",
        json={
            "base_url": node_server["url"],
            "global_parameters": parameters,
            "parameter_name": "target",
            "steps": [
                {
                    k: v
                    for k, v in s.items()
                    if k in allowed and not (k == "parent_step_id" and v is None)
                }
                for s in task["steps"]
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["value"] == 42
    assert sorted(c[0] for c in node_server["calls"]) == ["/left", "/right"]
    assert api.get(url + "/runs").json()["total"] == 0


@pytest.mark.parametrize(
    "content",
    [
        '<taskStep order="1"><parallel><taskStep order="1"><wait milliseconds="1"/></taskStep></parallel></taskStep>',
        '<taskStep order="1"><rest method="POST" path="/raw"><body/><rawBody/></rest></taskStep>',
        '<taskStep order="1"><rest method="GET" path="/value"><body><kv location="query"><key>id</key><value>1</value></kv></body></rest></taskStep>',
        '<taskStep order="2147483648"><wait milliseconds="1"/></taskStep>',
    ],
)
def test_invalid_xml_nodes_include_structural_location(content):
    with pytest.raises(BusinessError) as error:
        parse_upload("task.xml", ('<task name="nodes">' + content + "</task>").encode())
    issue = error.value.payload["issues"][0]
    assert "/task" in issue["location"] and "line" in issue["location"]


def test_parallel_sibling_parameter_dependencies_rejected():
    config = fixtures.bare_config()
    leaf = config["steps"][0]
    second = copy.deepcopy(leaf)
    second.update(id=2, position=2, parent_step_id=3)
    leaf.update(position=1, parent_step_id=3)
    second["headers"] = [{"name": "X-Id", "binding": {"kind": "global", "parameter_name": "id"}}]
    config["steps"] += [second, dict(parallel(), id=3)]
    config["global_parameters"] = [source("id", 0)]
    config["global_parameters"][0]["rest_source"] = {"step_id": 1, "extract_expression": "data.id"}
    # Header accepts string values by default; keep source string for the dependency check.
    config["global_parameters"][0]["value_type"] = "string"
    with pytest.raises(BusinessError) as error:
        validate_config(config, run=True)
    assert error.value.payload["code"] == "PARAMETER_NOT_AVAILABLE"


def test_collection_stops_pending_members_while_failed_member_is_cleaning(api, node_server):
    from test_task_collections import create_collection, wait_collection

    rest, create = assets(api, node_server)
    first_url, first = create([rest("/fail", 1), rest("/slow", 1, "finally")], name="first")
    members = [(first, 1)]
    for index in range(7):
        _, task = create([rest("/slow", 1)], name=f"active-{index}")
        members.append((task, 1))
    _, last = create([rest("/after", 1)], name="pending")
    members.append((last, 1))
    project_url = first_url.split("/tasks/")[0]
    group = create_collection(api, project_url, members)
    collection_url = project_url + f"/task-collections/{group['id']}"
    rid = api.post(collection_url + "/runs").json()["id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = api.get(collection_url + f"/runs/{rid}").json()
        last_result = next(m for m in result["members"] if m["task_id"] == last["id"])
        if last_result["status"] == "skipped":
            break
        time.sleep(0.01)
    assert last_result["status"] == "skipped"
    first_result = next(m for m in result["members"] if m["task_id"] == first["id"])
    detail = api.get(first_url + f"/runs/{first_result['task_run_id']}").json()
    assert detail["run"]["status"] == "running"
    assert detail["run"]["execution_stage"] == "finally"
    assert result["status"] == "running"
    node_server["released"].set()
    result = wait_collection(api, collection_url + f"/runs/{rid}")
    assert result["status"] == "failed"
    assert result["global_parameters"] == []
    assert all(c[0] != "/after" for c in node_server["calls"])


def test_wait_stop_is_interruptible_and_recovery_does_not_send_cleanup(database, node_server):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from app.service.task_execution import RunExecutor

    with database() as db:
        db.row_factory = dict_row
        pid = db.execute(
            "INSERT INTO projects(name) VALUES('interrupted') RETURNING id"
        ).fetchone()["id"]
        tid = db.execute(
            "INSERT INTO tasks(project_id,name,base_url) VALUES(%s,'wait',%s) RETURNING id",
            (pid, node_server["url"]),
        ).fetchone()["id"]
        config = {
            "base_url": node_server["url"],
            "global_parameters": [],
            "steps": [
                {
                    "id": 1,
                    "step_type": "wait",
                    "execution_scope": "main",
                    "position": 1,
                    "wait_milliseconds": 300000,
                },
                {
                    "id": 2,
                    "step_type": "wait",
                    "execution_scope": "finally",
                    "position": 1,
                    "wait_milliseconds": 1,
                },
            ],
        }
        rid = db.execute(
            "INSERT INTO task_runs(task_id,project_id,status,started_at,config_snapshot) VALUES(%s,%s,'running',now(),%s) RETURNING id",
            (tid, pid, Jsonb(config)),
        ).fetchone()["id"]
        for step in config["steps"]:
            db.execute(
                "INSERT INTO task_run_steps(run_id,step_id,position,step_type,execution_scope) VALUES(%s,%s,%s,%s,%s)",
                (rid, step["id"], step["position"], step["step_type"], step["execution_scope"]),
            )
    stop = threading.Event()
    worker = threading.Thread(target=RunExecutor(rid, stop).execute)
    worker.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with database() as db:
            status = db.execute(
                "SELECT status FROM task_run_steps WHERE run_id=%s AND step_id=1", (rid,)
            ).fetchone()[0]
        if status == "running":
            break
        time.sleep(0.01)
    stop.set()
    worker.join(2)
    assert not worker.is_alive()
    with database() as db:
        assert db.execute(
            "SELECT status,execution_stage,cleanup_status,error_code FROM task_runs WHERE id=%s",
            (rid,),
        ).fetchone() == ("failed", "finished", "skipped", "EXECUTION_INTERRUPTED")
        assert db.execute(
            "SELECT status FROM task_run_steps WHERE run_id=%s ORDER BY step_id", (rid,)
        ).fetchall() == [("failed",), ("skipped",)]
    assert node_server["calls"] == []


def test_missing_extraction_publishes_null_and_preserves_other_values(api, node_server):
    rest, create = assets(api, node_server)
    invalid = source("missing", 0)
    invalid["rest_source"]["extract_expression"] = "data.missing"
    needs = rest("/needs", 1, "finally")
    needs["bindings"] = [
        {"location": "query", "name": "id", "binding": {"kind": "global", "parameter_name": "id"}}
    ]
    url, _ = create(
        [rest("/value", 1), needs, rest("/cleanup", 2, "finally")], [source("id", 0), invalid]
    )
    result = wait_run(api, url, api.post(url + "/runs").json()["id"])
    assert result["run"]["status"] == "succeeded"
    assert {p["name"]: p["value"] for p in result["global_parameters"]} == {"id": 42, "missing": None}
    assert all(p["published"] for p in result["global_parameters"])
    assert result["steps"][1]["status"] == "succeeded"
    assert [c[0] for c in node_server["calls"]] == ["/value", "/needs?id=42", "/cleanup"]


def test_xml_import_preserves_tree_order_raw_text_and_sources(api, node_server):
    _rest, _create = assets(api, node_server)
    # Locate the project created by assets without creating a task.
    project = api.get("/api/v1/projects").json()["items"][0]
    base = f"/api/v1/projects/{project['id']}"
    xml = """<task name="xml-tree">
      <taskStep order="1"><wait milliseconds="1"/></taskStep>
      <taskStep order="20"><parallel maxConcurrency="2">
        <taskStep order="1"><rest method="GET" path="/value"><extractParam valueType="integer"><name>id</name><value>data.id</value></extractParam></rest></taskStep>
        <taskStep order="2"><rest method="POST" path="/raw"><rawBody><![CDATA[  {"name":中文 {token}  ]]></rawBody><assertionCode><![CDATA[
def assert_response(responseBody, globalParam, statusCode, headers):
    return statusCode == 400 and responseBody["code"] == "INVALID_JSON"
]]></assertionCode></rest></taskStep>
      </parallel></taskStep>
      <finally><taskStep order="1"><rest method="GET" path="/cleanup"/></taskStep></finally>
    </task>"""
    response = api.post(
        base + "/tasks/import",
        data={"base_url": node_server["url"]},
        files={"file": ("task.xml", xml.encode(), "application/xml")},
    )
    task = schema_check_list(response, "Task", 201)[0]
    assert [s["step_type"] for s in task["steps"]] == ["wait", "parallel", "rest", "rest", "rest"]
    assert [s["position"] for s in task["steps"]] == [1, 20, 1, 2, 1]
    assert task["steps"][2]["parent_step_id"] == task["steps"][1]["id"]
    assert task["global_parameters"][0]["rest_source"]["step_id"] == task["steps"][2]["id"]
    assert task["steps"][3]["raw_body"]["text"] == '  {"name":中文 {token}  '
    assert node_server["calls"] == []
    url = base + f"/tasks/{task['id']}"
    result = wait_run(api, url, api.post(url + "/runs").json()["id"])
    assert result["run"]["status"] == "succeeded"
    assert result["global_parameters"][0]["published"]


def test_parallel_no_complete_responses_has_no_zero_samples_and_stable_error(
    api, node_server, monkeypatch
):
    rest, create = assets(api, node_server)
    barrier = threading.Barrier(2)

    def timeout(request, timeout):
        barrier.wait(3)
        request["duration_ms"] = 123.456
        return {
            "response_status": None,
            "response_headers": None,
            "response_body": None,
            "response_body_encoding": None,
            "error_code": "TIMEOUT",
            "error_message": "请求超时",
        }

    monkeypatch.setattr("app.service.task_execution.request_http", timeout)
    url, task = create([parallel(), rest("/left", 10, parent=0), rest("/right", 2, parent=0)])
    rid = api.post(url + "/runs").json()["id"]
    result = wait_run(api, url, rid)
    stats = result["steps"][0]["parallel_stats"]
    assert stats["request_count"] == stats["failed_count"] == 2
    assert stats["response_count"] == stats["response_time_sum_ms"] == 0
    assert (
        stats["avg_response_time_ms"]
        is stats["min_response_time_ms"]
        is stats["max_response_time_ms"]
        is None
    )
    first_child = next(s for s in task["steps"] if s.get("parent_step_id") and s["position"] == 2)
    assert result["steps"][0]["failed_child_step_id"] == first_child["id"]
    assert result["steps"][0]["child_error_code"] == "TIMEOUT"
    assert api.get(url + f"/runs/{rid}/requests").json()["total"] == 0


def test_fixed_initialization_failure_still_attempts_cleanup(
    api, database, node_server, monkeypatch
):
    from psycopg.types.json import Jsonb

    from app.service.task_execution import RunExecutor

    rest, create = assets(api, node_server)
    url, _ = create(
        [rest("/after", 1), rest("/cleanup", 1, "finally")],
        [{"name": "fixed", "source_type": "fixed", "value_type": "integer", "value": 1}],
    )
    execute = RunExecutor.execute
    monkeypatch.setattr(RunExecutor, "execute", lambda self: None)
    rid = api.post(url + "/runs").json()["id"]
    with database() as db:
        config = db.execute(
            "SELECT config_snapshot FROM task_runs WHERE id=%s FOR UPDATE", (rid,)
        ).fetchone()[0]
        config["global_parameters"][0]["value"] = "invalid-integer"
        db.execute(
            "UPDATE task_runs SET status='running',started_at=now(),config_snapshot=%s WHERE id=%s",
            (Jsonb(config), rid),
        )
    execute(RunExecutor(rid))
    result = wait_run(api, url, rid)
    assert result["run"]["primary_error"]["code"] == "PARAMETER_INITIALIZATION_FAILED"
    assert result["run"]["cleanup_status"] == "succeeded"
    assert [c[0] for c in node_server["calls"]] == ["/cleanup"]
    assert result["global_parameters"][0]["status"] == "failed"
    assert not result["global_parameters"][0]["published"]


def test_interrupted_parallel_preserves_samples_and_only_reclassifies_unfinished_nodes(database):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from app.service.task_execution import finish_failure
    from app.service.task_nodes import empty_stats

    with database() as db:
        db.row_factory = dict_row
        pid = db.execute(
            "INSERT INTO projects(name) VALUES('recover-stats') RETURNING id"
        ).fetchone()["id"]
        tid = db.execute(
            "INSERT INTO tasks(project_id,name,base_url) VALUES(%s,'old','http://localhost') RETURNING id",
            (pid,),
        ).fetchone()["id"]
        rid = db.execute(
            "INSERT INTO task_runs(task_id,project_id,status,started_at,config_snapshot) VALUES(%s,%s,'running',now(),'{}') RETURNING id",
            (tid, pid),
        ).fetchone()["id"]
        stats = empty_stats(3)
        stats.update(
            pending_count=1,
            running_count=1,
            succeeded_count=1,
            request_count=2,
            response_count=1,
            response_time_sum_ms=12.345,
            avg_response_time_ms=12.345,
            min_response_time_ms=12.345,
            max_response_time_ms=12.345,
        )
        parent = db.execute(
            "INSERT INTO task_run_steps(run_id,step_id,position,step_type,execution_scope,status,parallel_stats) VALUES(%s,1,1,'parallel','main','running',%s) RETURNING id",
            (rid, Jsonb(stats)),
        ).fetchone()["id"]
        for index, status in enumerate(("succeeded", "running", "pending"), 1):
            db.execute(
                "INSERT INTO task_run_steps(run_id,step_id,position,step_type,execution_scope,parent_run_step_id,status) VALUES(%s,%s,%s,'rest','main',%s,%s)",
                (rid, index + 1, index, parent, status),
            )
        finish_failure(db, rid, "EXECUTION_INTERRUPTED", "执行中断")
        result = db.execute(
            "SELECT parallel_stats FROM task_run_steps WHERE id=%s", (parent,)
        ).fetchone()["parallel_stats"]
        assert result["succeeded_count"] == result["failed_count"] == result["skipped_count"] == 1
        assert result["pending_count"] == result["running_count"] == 0
        assert result["response_count"] == 1 and result["request_count"] == 2
        assert result["response_time_sum_ms"] == result["avg_response_time_ms"] == 12.345
        assert (
            db.execute(
                "SELECT count(*) AS n FROM task_requests WHERE run_id=%s", (rid,)
            ).fetchone()["n"]
            == 0
        )


def test_task_uuid_shared_by_steps_and_regenerated_on_rerun(api, node_server):
    from uuid import UUID

    rest, create = assets(api, node_server)
    header = [{"name": "X-Task-UUID", "binding": {"kind": "global", "parameter_name": "task_uuid"}}]
    nodes = [rest("/value", 1), rest("/after", 2)]
    for node in nodes:
        node["headers"] = header
    url, task = create(
        nodes,
        [
            {
                "name": "task_uuid",
                "source_type": "function",
                "value_type": "string",
                "value": "generateUUID()",
            }
        ],
    )
    parameters = [{"name": "task_uuid", "source_type": "function", "value_type": "string", "value": "generateUUID()"}, {"name": "literal", "source_type": "fixed", "value_type": "string", "value": "generateUUID()"}]
    response = api.put(url, json={"name": task["name"], "base_url": node_server["url"], "global_parameters": parameters, "steps": nodes})
    assert response.status_code == 200, response.text
    copied = api.post(url + "/copy", json={"name": "uuid-copy"})
    assert copied.status_code == 201, copied.text
    assert copied.json()["global_parameters"][0]["source_type"] == "function"
    assert copied.json()["global_parameters"][0]["value"] == "generateUUID()"
    generated = []
    copied_url = url.rsplit("/", 1)[0] + f"/{copied.json()['id']}"
    for run_url in (url, url, copied_url):
        rid = api.post(run_url + "/runs").json()["id"]
        deadline = time.monotonic() + 10
        while True:
            response = api.get(run_url + f"/runs/{rid}")
            assert response.status_code == 200
            result = response.json()
            if result["run"]["status"] in {"succeeded", "failed", "blocked", "stopped"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert result["run"]["status"] == "succeeded"
        parameters = {p["name"]: p["value"] for p in result["global_parameters"]}
        assert parameters["literal"] == "generateUUID()"
        value = parameters["task_uuid"]
        assert UUID(value).version == 4
        assert [call[2]["X-Task-UUID"] for call in node_server["calls"][-2:]] == [value, value]
        generated.append(value)
    assert len(set(generated)) == 3
    saved = api.get(url).json()
    assert saved["global_parameters"][0]["value"] == "generateUUID()"
