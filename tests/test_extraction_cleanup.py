"""Response assignments survive failed assertions and remain available to cleanup."""

import copy
import json
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from app.service.task_execution import RunExecutor, extract_values, preview


def config(parallel=False, assertion="return statusCode == 400", expression="data.token"):
    register = {
        "id": 1,
        "step_type": "rest",
        "execution_scope": "main",
        "position": 1,
        "interface": {
            "method": "POST",
            "path": "/register",
            "parameters": [],
            "request_body": None,
        },
        "document": {},
        "bindings": [],
        "headers": [],
        "assertion_code": (
            "def assert_response(responseBody, globalParam, statusCode, headers):\n    " + assertion
        ),
    }
    cleanup = {
        "id": 2,
        "step_type": "rest",
        "execution_scope": "finally",
        "position": 1,
        "interface": {
            "method": "DELETE",
            "path": "/account",
            "parameters": [],
            "request_body": None,
        },
        "document": {},
        "bindings": [],
        "headers": [
            {"name": "Authorization", "binding": {"kind": "global", "parameter_name": "token"}}
        ],
    }
    steps = [register, cleanup]
    if parallel:
        register["parent_step_id"] = 3
        steps += [
            {
                "id": 3,
                "step_type": "parallel",
                "execution_scope": "main",
                "position": 1,
                "max_concurrency": 1,
            },
            {
                "id": 4,
                "step_type": "wait",
                "execution_scope": "main",
                "position": 2,
                "parent_step_id": 3,
                "wait_milliseconds": 1,
            },
        ]
    return {
        "base_url": "http://localhost",
        "steps": steps,
        "global_parameters": [
            {
                "id": 10,
                "name": "token",
                "source_type": "rest",
                "value_type": "string",
                "sensitive": False,
                "rest_source": {"step_id": 1, "extract_expression": expression},
            }
        ],
    }


def response(body=None, code=None):
    return {
        "response_status": 201,
        "response_headers": [],
        "response_body": json.dumps(body if body is not None else {"data": {"token": "TOKEN"}}),
        "response_body_encoding": "utf-8",
        "error_code": code,
        "error_message": code,
    }


@pytest.fixture
def runtime(monkeypatch):
    task = config()
    db = MagicMock()
    db.__enter__.return_value = db
    db.__exit__.return_value = False

    def execute(sql, arguments=()):
        row = {"id": 100, "run_uuid": uuid4(), "status": "running", "config_snapshot": task}
        if sql.startswith("SELECT id,parallel_stats"):
            return Mock(fetchall=list)
        return Mock(fetchone=lambda: row)

    db.execute.side_effect = execute
    monkeypatch.setattr("app.service.task_execution.connection", lambda: db)
    monkeypatch.setattr(RunExecutor, "stats_transition", lambda *args, **kwargs: None)
    requests = []

    def send(request, timeout):
        requests.append(copy.deepcopy(request))
        request["duration_ms"] = 1
        return response()

    monkeypatch.setattr("app.service.task_execution.request_http", send)
    return task, db, requests


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize(
    "assertion,error",
    [
        ("return statusCode == 400", "ASSERTION_FAILED"),
        ("raise ValueError('secret')", "ASSERTION_EXECUTION_ERROR"),
        ("return 1", "ASSERTION_RETURN_TYPE_ERROR"),
    ],
)
def test_failed_assertion_publishes_token_and_cleanup_sends_it(runtime, parallel, assertion, error):
    task, db, requests = runtime
    task.update(config(parallel=parallel, assertion=assertion))
    executor = RunExecutor(1)
    executor.execute()
    assert executor.values == {"token": "TOKEN"}
    assert [r["method"] for r in requests] == ["POST", "DELETE"]
    assert requests[-1]["request_headers"] == [{"name": "Authorization", "value": "TOKEN"}]
    assignments = [
        call.args[1]
        for call in db.execute.call_args_list
        if call.args[0].startswith("UPDATE task_run_parameters SET status=%s,value=")
    ]
    assert assignments[0][0] == "succeeded"
    assert assignments[0][1].obj == "TOKEN"
    assert assignments[0][4:6] == (None, None)
    final = [
        call.args[1]
        for call in db.execute.call_args_list
        if call.args[0].startswith("UPDATE task_runs SET status=%s,execution_stage='finished'")
    ]
    assert final[-1][0:2] == ("failed", "succeeded")
    assert final[-1][2] == ("PARALLEL_FAILED" if parallel else error)
    if parallel:
        assert any(
            call.args[0].startswith("UPDATE task_run_parameters SET published=true")
            for call in db.execute.call_args_list
        )
    else:
        assert assignments[0][2] is True


@pytest.mark.parametrize("body", [{"data": {}}, {"data": {"token": 7}}, {"data": {"token": None}}])
def test_missing_or_wrong_type_assigns_null_without_losing_other_values(body):
    task = config()
    other = copy.deepcopy(task["global_parameters"][0])
    other.update(id=11, name="found", value_type="integer")
    other["rest_source"]["extract_expression"] = "id"
    task["global_parameters"].append(other)
    assert extract_values(task, task["steps"][0], response(body | {"id": 42})) == {
        "token": None,
        "found": 42,
    }


@pytest.mark.parametrize("error", [None, "HTTP_STATUS_ERROR", "TIMEOUT", "CONNECTION_ERROR"])
def test_unusable_response_assigns_null(error):
    task = config()
    incoming = response(code=error)
    incoming["response_body"] = "not-json"
    assert extract_values(task, task["steps"][0], incoming) == {"token": None}


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("expression,expected", [("data.token", "***"), ("missing", None)])
def test_preview_returns_assignment_despite_failed_assertion(
    runtime, parallel, expression, expected
):
    task, _db, requests = runtime
    task.update(config(parallel=parallel, expression=expression))
    assert preview(task, "token")["value"] == expected
    assert [r["method"] for r in requests] == ["POST"]


def test_null_fallback_is_persisted_as_assignment(runtime):
    task, db, _requests = runtime
    task.update(config(assertion="return True", expression="missing"))
    executor = RunExecutor(1)
    executor.execute()
    assert executor.values == {"token": None}
    assignments = [
        call.args[1]
        for call in db.execute.call_args_list
        if call.args[0].startswith("UPDATE task_run_parameters SET status=%s,value=")
    ]
    assert assignments[0][0] == "succeeded"
    assert assignments[0][1].obj is None
    assert assignments[0][2] is True
