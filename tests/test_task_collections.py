"""Task collection contracts and real PostgreSQL/HTTP batch execution."""

import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_task_business as fixtures
from openapi_schema_validator import OAS30Validator
from test_task_business import asset, bare_config

from app.api.contract import load_contract
from app.config.settings import ROOT
from app.service.errors import BusinessError
from app.service.task_collections import validate_members

api, database, server = fixtures.api, fixtures.database, fixtures.server

SPEC = load_contract(ROOT / "docs/ rest-api/rest-task-collection.yaml")


def checked(response, schema, status=200):
    from jsonschema import RefResolver

    assert response.status_code == status, response.text
    OAS30Validator(
        SPEC["components"]["schemas"][schema], resolver=RefResolver.from_schema(SPEC)
    ).validate(response.json())
    return response.json()


def create_collection(api, p, tasks, name="collection"):
    return checked(
        api.post(
            p + "/task-collections",
            json={
                "name": name,
                "members": [{"task_id": t["id"], "position": position} for t, position in tasks],
            },
        ),
        "Collection",
        201,
    )


def wait_collection(api, url):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = checked(api.get(url), "RunDetail")
        if result["status"] in {"succeeded", "failed"}:
            return result
        time.sleep(0.02)
    pytest.fail("collection did not finish")


def task_body(task):
    body = {k: copy.deepcopy(task[k]) for k in ["name", "base_url", "global_parameters"]}
    body["global_parameters"] = [
        {
            k: copy.deepcopy(p[k])
            for k in [
                "name",
                "value_type",
                "source_type",
                "sensitive",
                "value",
                "rest_source",
            ]
            if k in p
        }
        for p in task["global_parameters"]
    ]
    body["steps"] = [
        {
            k: copy.deepcopy(s[k])
            for k in [
                "id",
                "interface_id",
                "file_id",
                "version",
                "bindings",
                "assertion_code",
            ]
            if k in s
        }
        for s in task["steps"]
    ]
    return body


def configure(api, url, task, value, sensitive=False, assertion=None):
    body = task_body(task)
    body["global_parameters"] = [
        {
            "name": "token",
            "value_type": "string",
            "value": value,
            "source_type": "fixed",
            "sensitive": sensitive,
        }
    ]
    if assertion:
        body["steps"][0]["assertion_code"] = assertion
    response = api.put(url, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_collection_contract_examples_and_static_dependencies():
    from jsonschema import RefResolver
    from openapi_spec_validator import validate

    validate(SPEC)
    for schema in SPEC["components"]["schemas"].values():
        if "example" in schema:
            OAS30Validator(schema, resolver=RefResolver.from_schema(SPEC)).validate(
                schema["example"]
            )
    config = bare_config()
    config["global_parameters"] = [
        {
            "name": "token",
            "value_type": "string",
            "sensitive": False,
            "source_type": "rest",
            "rest_source": {"step_id": 1, "extract_expression": "data.b"},
        }
    ]
    config["steps"][0]["bindings"] = [
        {
            "location": "header",
            "name": "X-Token",
            "binding": {"kind": "global", "parameter_name": "token"},
        }
    ]
    first = bare_config()
    first["global_parameters"] = [
        {
            "name": "token",
            "source_type": "fixed",
            "value_type": "string",
            "sensitive": True,
            "value": "secret",
        }
    ]
    for position in [1, 2]:
        group = [
            {"position": 1, "config": copy.deepcopy(first)},
            {"position": position, "config": copy.deepcopy(config)},
        ]
        if position == 1:
            with pytest.raises(BusinessError):
                validate_members(group)
        else:
            validate_members(group)
            assert group[1]["config"]["collection_available"] == ["token"]
            assert group[1]["config"]["global_parameters"][0]["sensitive"]
    invalid = copy.deepcopy(first)
    invalid["global_parameters"][0].update(value_type="integer", value=1)
    with pytest.raises(BusinessError):
        validate_members([{"position": 1, "config": first}, {"position": 2, "config": invalid}])


def test_collection_crud_ownership_validation_and_removal(api, server, database):
    p, _, _, create = asset(api, server)
    _, task = create(["/ok"])
    base = p + "/task-collections"
    c = create_collection(api, p, [(task, 8)])
    url = base + f"/{c['id']}"
    assert api.post(base, json={"name": " collection ", "members": []}).status_code == 409
    for members in [
        [{"task_id": task["id"], "position": 1}] * 2,
        [{"task_id": task["id"], "position": 0}],
        [{"task_id": 2147483647, "position": 1}],
    ]:
        assert api.put(url, json={"name": "bad", "members": members}).status_code == 400
    foreign = api.post("/api/v1/projects", json={"name": "foreign"}).json()["id"]
    assert api.get(f"/api/v1/projects/{foreign}/task-collections/{c['id']}").status_code == 404
    assert (
        api.post(
            f"/api/v1/projects/{foreign}/task-collections",
            json={
                "name": "foreign-member",
                "members": c["members"],
            },
        ).status_code
        == 400
    )
    assert checked(api.get(base), "CollectionPage")["total"] == 1
    assert api.delete(p + f"/tasks/{task['id']}?confirm=true").status_code == 409
    assert api.get(p + f"/tasks/{task['id']}/deletion-preview").json()["can_delete"] is False
    assert api.delete(url).status_code == 400
    assert api.put(url, json={"name": "empty", "members": []}).status_code == 200
    assert api.post(url + "/runs").status_code == 400
    with database() as db:
        assert db.execute("SELECT count(*) FROM task_collection_runs").fetchone()[0] == 0
    assert api.delete(url + "?confirm=true").status_code == 204
    assert api.get(url).status_code == 404


def test_batch_parallelism_deterministic_merge_logs_reservations_and_history(
    api,
    server,
    database,
    caplog,
):
    p, _, _, create = asset(api, server)
    a_url, a = create(["/slow"])
    b_url, b = create(["/slow"])
    c_url, c = create(["/ok"])
    a = configure(api, a_url, a, "SECRET_A", sensitive=True)
    b = configure(api, b_url, b, "SECRET_B")
    c = configure(api, c_url, c, "SECRET_C")
    group = create_collection(api, p, [(b, 1), (a, 1), (c, 9)])
    url = p + f"/task-collections/{group['id']}"
    run = checked(api.post(url + "/runs"), "Run", 202)
    detail_url = url + f"/runs/{run['id']}"
    try:
        deadline = time.monotonic() + 2
        while len(server["calls"]) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(server["calls"]) == 2  # both slow requests entered before release
        assert all(q["path"].endswith("/slow") for q in server["calls"])
        for task_url in [a_url, b_url, c_url]:
            assert api.post(task_url + "/runs").status_code == 409
        assert (
            api.put(
                a_url,
                json=task_body(a),
            ).status_code
            == 409
        )
        assert api.put(url, json={"name": "changed", "members": []}).status_code == 409
        assert api.delete(url + "?confirm=true").status_code == 409
        assert api.delete(p + "?confirm=true").status_code == 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert all(
                r.status_code == 409 for r in pool.map(lambda _: api.post(url + "/runs"), range(2))
            )
    finally:
        server["release"].set()
    result = wait_collection(api, detail_url)
    assert result["status"] == "succeeded"
    assert len(server["calls"]) == 3 and server["calls"][-1]["path"].endswith("/ok")
    token = result["global_parameters"][0]
    assert token["source_task_id"] == c["id"] and token["value"] == "***" and token["sensitive"]
    overrides = checked(api.get(detail_url + "/overrides"), "OverridePage")
    assert [(r["old_task_id"], r["new_task_id"]) for r in overrides["items"]] == [
        (a["id"], b["id"]),
        (b["id"], c["id"]),
    ]
    deadline = time.monotonic() + 2
    while "PARAMETER_OVERRIDDEN" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "PARAMETER_OVERRIDDEN" in caplog.text
    assert "SECRET_" not in caplog.text
    for m in result["members"]:
        response = api.get(p + f"/tasks/{m['task_id']}/runs/{m['task_run_id']}")
        assert response.status_code == 200 and "SECRET_" not in response.text
    with database() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM task_collection_parameter_overrides WHERE logged_at IS NULL"
            ).fetchone()[0]
            == 0
        )
    assert api.delete(url + "?confirm=true").status_code == 204
    assert checked(api.get(url + "/runs"), "RunPage")["total"] == 1
    assert api.get(detail_url).status_code == 200
    assert api.delete(a_url + "?confirm=true").status_code == 200
    m = result["members"][0]
    assert api.get(p + f"/tasks/{m['task_id']}/runs/{m['task_run_id']}").status_code == 200
    assert api.delete(p + "?confirm=true").status_code == 200
    with database() as db:
        for table in [
            "task_collection_runs",
            "task_collection_run_members",
            "task_runs",
            "task_requests",
        ]:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_failure_batch_does_not_publish_partial_values_or_execute_later_batches(api, server):
    p, _, _, create = asset(api, server)
    a_url, a = create(["/ok"])
    b_url, b = create(["/fail"])
    c_url, c = create(["/ok"])
    d_url, d = create(["/ok"])
    a = configure(api, a_url, a, "first")
    b = configure(api, b_url, b, "failed")
    c = configure(api, c_url, c, "partial")
    d = configure(api, d_url, d, "later")
    group = create_collection(api, p, [(a, 1), (b, 2), (c, 2), (d, 3)])
    url = p + f"/task-collections/{group['id']}/runs"
    run = checked(api.post(url), "Run", 202)
    result = wait_collection(api, url + f"/{run['id']}")
    assert result["status"] == "failed"
    assert result["global_parameters"][0]["value"] == "first"
    assert result["members"][-1]["status"] == "skipped"
    assert len(server["calls"]) in [2, 3]
    skipped = result["members"][-1]
    detail = api.get(d_url + f"/runs/{skipped['task_run_id']}").json()
    assert detail["steps"][0]["status"] == "skipped"
    assert detail["run"]["error_code"] == "COLLECTION_MEMBER_SKIPPED"


def test_inherited_parameters_read_before_current_response_and_types(api, server, database):
    p, _, _, create = asset(api, server)
    a_url, a = create(["/ok"])
    b_url, b = create(["/init"])
    a = configure(api, a_url, a, "secret", sensitive=True)
    source = b["steps"][0]["id"]
    # Assertions execute before extraction, and can consume a previous batch value.
    body = task_body(b)
    body["global_parameters"] = [
        {
            "name": "token",
            "source_type": "rest",
            "value_type": "string",
            "sensitive": False,
            "rest_source": {"step_id": source, "extract_expression": "data.b"},
        }
    ]
    body["steps"][0]["assertion_code"] = (
        'def assert_response(responseBody, globalParam):\n    return globalParam["token"] == "secret"'
    )
    body["steps"][0]["bindings"] = [
        {
            "location": "header",
            "name": "X-Token",
            "binding": {"kind": "global", "parameter_name": "token"},
        }
    ]
    response = api.put(b_url, json=body)
    assert response.status_code == 200, response.text
    b = response.json()
    assert api.post(b_url + "/runs").status_code == 400
    bad = create_collection(api, p, [(a, 1), (b, 1)], name="same-batch")
    assert api.post(p + f"/task-collections/{bad['id']}/runs").status_code == 400
    group = create_collection(api, p, [(a, 1), (b, 2)])
    url = p + f"/task-collections/{group['id']}/runs"
    run = checked(api.post(url), "Run", 202)
    result = wait_collection(api, url + f"/{run['id']}")
    assert result["status"] == "succeeded"
    m = result["members"][1]
    requests = api.get(b_url + f"/runs/{m['task_run_id']}/requests")
    assert "PRIVATE_TOKEN" not in requests.text
    assert server["calls"][-1]["headers"]["X-Token"] == "secret"
    assert '"value":"secret"' not in requests.text
    assert result["global_parameters"][0]["sensitive"]
    assert result["global_parameters"][0]["source_task_id"] == b["id"]


def test_restart_interrupts_collection_and_children_without_requests(database, server):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.repository.projects import Projects
    from app.repository.rest_task_collection import createTaskCollection, startTaskCollectionRun
    from app.service.task_execution import Dispatcher

    with TestClient(create_app()) as client:
        _, _, _, create = asset(client, server)
        _, task = create(["/ok"])
    with database() as db:
        repository = Projects(db)
        path = {"project_id": task["project_id"]}
        c = createTaskCollection(
            repository,
            path,
            {},
            {
                "name": "restart",
                "members": [
                    {"task_id": task["id"], "position": 1},
                ],
            },
            None,
        )
        path["collection_id"] = c["id"]
        run = startTaskCollectionRun(repository, path, {}, {}, None)
    dispatcher = Dispatcher()
    dispatcher.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with database() as db:
                row = db.execute(
                    "SELECT status,error_code FROM task_collection_runs WHERE id=%s", (run["id"],)
                ).fetchone()
            if row[0] == "failed":
                break
            time.sleep(0.02)
        assert row == ("failed", "EXECUTION_INTERRUPTED")
        assert not server["calls"]
        with database() as db:
            assert db.execute("SELECT status,error_code FROM task_runs").fetchone() == (
                "failed",
                "EXECUTION_INTERRUPTED",
            )
    finally:
        dispatcher.close()


def test_collection_and_independent_runs_share_eight_slots(api, server, database):
    p, _, _, create = asset(api, server)
    independent, _ = create(["/slow"])
    standalone = api.post(independent + "/runs")
    assert standalone.status_code == 202
    tasks = [create(["/slow"])[1] for _ in range(10)]
    group = create_collection(api, p, [(task, 1) for task in tasks])
    url = p + f"/task-collections/{group['id']}/runs"
    run = checked(api.post(url), "Run", 202)
    try:
        deadline = time.monotonic() + 2
        while len(server["calls"]) < 8 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(server["calls"]) == 8
        time.sleep(0.15)
        assert len(server["calls"]) == 8
        with database() as db:
            statuses = dict(
                db.execute(
                    "SELECT status,count(*) FROM task_collection_run_members GROUP BY status"
                ).fetchall()
            )
        assert statuses == {"running": 7, "pending": 3}
    finally:
        server["release"].set()
    result = wait_collection(api, url + f"/{run['id']}")
    assert result["status"] == "succeeded" and len(server["calls"]) == 11


def test_completed_member_remains_reserved_until_collection_finishes(api, server):
    p, _, _, create = asset(api, server)
    first_url, first = create(["/ok"])
    _, last = create(["/slow"])
    group = create_collection(api, p, [(first, 1), (last, 2)])
    url = p + f"/task-collections/{group['id']}/runs"
    run = checked(api.post(url), "Run", 202)
    try:
        assert server["entered"].wait(2)
        detail = checked(api.get(url + f"/{run['id']}"), "RunDetail")
        assert detail["members"][0]["status"] == "succeeded"
        assert api.post(first_url + "/runs").status_code == 409
        rows = api.get(p + "/tasks").json()["items"]
        assert all(t["has_active_run"] for t in rows)
        assert api.get(p + "/deletion-preview").json()["can_delete"] is False
    finally:
        server["release"].set()
    assert wait_collection(api, url + f"/{run['id']}")["status"] == "succeeded"
    assert api.post(first_url + "/runs").status_code == 202
