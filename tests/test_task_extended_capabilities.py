"""Integration of XML capabilities with parameter binding and bounded execution."""

import json
import sqlite3
import threading
from unittest.mock import MagicMock

import pytest

from app.service.errors import BusinessError
from app.service.task_config import validate_config
from app.service.task_execution import extract_values, request_with_poll
from app.service.task_expressions import compute_values
from app.service.task_import import convert, parse_upload
from app.service.task_semantics import validate_options
from app.service.task_sql import execute_sql


def imported(xml):
    config = convert(parse_upload("task.xml", xml.encode()), "http://localhost", None)
    for index, step in enumerate(config["steps"]):
        step["id"] = index + 1
    for parameter in config["global_parameters"]:
        source = parameter.get("expression_source")
        if source:
            source["step_id"] = config["steps"][source.pop("step_index")]["id"]
    return validate_config(config)


def test_compute_to_sql_binding_does_not_interpolate(tmp_path):
    path = tmp_path / "values.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE items (name TEXT)")
    config = imported(f"""<task name="bind"><parameters>
      <parameter type="fixed"><name>suffix</name><value>'); DROP TABLE items; --</value></parameter>
      <parameter type="fixed" valueType="integer"><name>id</name><value>42</value></parameter>
      </parameters><taskStep order="1"><compute><parameter><name>name</name>
      <value>concat("order-", str(param("id")), param("suffix"))</value>
      </parameter></compute></taskStep><taskStep order="2"><sql driver="sqlite">
      <datasource>{path}</datasource><statement>INSERT INTO items VALUES (?)</statement>
      <parameters><parameter>name</parameter></parameters></sql></taskStep></task>""")
    values = {p["name"]: p["value"] for p in config["global_parameters"] if "value" in p}
    values.update(compute_values(config, config["steps"][0], values))
    execute_sql(config["steps"][1], values)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM items").fetchall() == [
            ("order-42'); DROP TABLE items; --",)
        ]
    with pytest.raises(BusinessError) as error:
        execute_sql(config["steps"][1], {})
    assert error.value.payload["code"] == "PARAMETER_NOT_AVAILABLE"


def test_postgres_parameter_values_passed_separately(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr("app.service.task_sql.psycopg.connect", connect)
    config = imported("""<task name="pg"><parameters><parameter type="fixed">
      <name>name</name><value>test</value></parameter></parameters><taskStep order="1">
      <sql driver="postgresql"><datasource>host=localhost</datasource>
      <statement>SELECT %s, %s</statement><parameters><parameter>name</parameter>
      <parameter>name</parameter></parameters></sql></taskStep></task>""")
    execute_sql(config["steps"][0], {"name": "' OR 1=1"})
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    assert cursor.execute.call_args.args == ("SELECT %s, %s", ("' OR 1=1", "' OR 1=1"))


def test_sql_rejects_future_parameter():
    with pytest.raises(BusinessError):
        imported("""<task name="future"><taskStep order="1"><sql driver="sqlite">
        <datasource>:memory:</datasource><statement>SELECT ?</statement>
        <parameters><parameter>later</parameter></parameters></sql></taskStep>
        <taskStep order="2"><compute><parameter><name>later</name><value>"x"</value>
        </parameter></compute></taskStep></task>""")


@pytest.mark.parametrize(
    "expression", ['concat("x", 1)', "str(None)", 'concat("x", param("bad"))', 'str(param("bad"))']
)
def test_concat_rejects_invalid_values(expression):
    step = {"options": {"expressions": [{"name": "out", "expression": expression}]}}
    config = {"global_parameters": [{"name": "out", "value_type": "string"}]}
    with pytest.raises(BusinessError) as error:
        compute_values(config, step, {"bad": [1, 2]})
    assert error.value.payload["code"] == "EXPRESSION_EVALUATION_ERROR"


def test_concat_bounded_result():
    step = {"options": {"expressions": [{"name": "out", "expression": 'concat(param("x"), "!")'}]}}
    config = {"global_parameters": [{"name": "out", "value_type": "string"}]}
    with pytest.raises(BusinessError):
        compute_values(config, step, {"x": "x" * 65536})


def response(done=False, error=None):
    return {
        "response_status": 200,
        "response_headers": [],
        "response_body": json.dumps({"done": done}),
        "response_body_encoding": "utf-8",
        "error_code": error,
        "error_message": "failure" if error else None,
    }


def poll_step(**policy):
    return {
        "id": 1,
        "step_type": "rest",
        "execution_scope": "main",
        "position": 1,
        "options": {"poll": {"max_attempts": 3, "interval_milliseconds": 1, **policy}},
        "assertion_code": 'def assert_response(responseBody, globalParam, statusCode, headers):\n    return responseBody["done"]',
    }


def test_poll_success_and_final_extraction(monkeypatch):
    step = poll_step()
    responses = iter([response(), response(True)])
    calls = []

    def send(request, timeout):
        calls.append(timeout)
        request["duration_ms"] = 7
        return next(responses)

    monkeypatch.setattr("app.service.task_execution.request_http", send)
    request = {}
    result = request_with_poll({}, step, {}, request)
    assert result["error_code"] is None
    assert len(calls) == 2
    assert request["duration_ms"] == 14
    config = {
        "steps": [step],
        "global_parameters": [
            {
                "name": "done",
                "source_type": "rest",
                "value_type": "boolean",
                "rest_source": {"step_id": 1, "extract_expression": "done"},
            }
        ],
    }
    assert extract_values(config, step, result) == {"done": True}


def test_poll_exhaustion_preserves_final_response(monkeypatch):
    send = MagicMock(return_value=response())
    monkeypatch.setattr("app.service.task_execution.request_http", send)
    result = request_with_poll({}, poll_step(), {}, {})
    assert result["error_code"] == "POLL_EXHAUSTED"
    assert json.loads(result["response_body"]) == {"done": False}
    assert send.call_count == 3


@pytest.mark.parametrize("code", ["CONNECTION_ERROR", "TIMEOUT", "PRECONDITION_BLOCKED"])
def test_poll_does_not_retry_terminal_errors(monkeypatch, code):
    send = MagicMock(return_value=response(error=code))
    monkeypatch.setattr("app.service.task_execution.request_http", send)
    assert request_with_poll({}, poll_step(), {}, {})["error_code"] == code
    assert send.call_count == 1


def test_poll_interruption_during_interval(monkeypatch):
    stop = threading.Event()

    def send(request, timeout):
        stop.set()
        return response()

    monkeypatch.setattr("app.service.task_execution.request_http", send)
    with pytest.raises(BusinessError) as error:
        request_with_poll({}, poll_step(interval_milliseconds=10000), {}, {}, stop)
    assert error.value.payload["code"] == "EXECUTION_INTERRUPTED"


@pytest.mark.parametrize(
    "patch",
    [{"max_attempts": 0}, {"max_attempts": True}, {"timeout_seconds": 86401}, {"unknown": 1}],
)
def test_invalid_poll(patch):
    with pytest.raises(BusinessError):
        validate_options(poll_step(**patch), {})


def test_poll_xml_import():
    root = parse_upload(
        "poll.xml",
        b"""<task name="poll"><taskStep order="1">
      <rest method="GET" path="/status"><poll maxAttempts="4" intervalMilliseconds="5"
      timeoutSeconds="6"/></rest></taskStep></task>""",
    )
    config = convert(
        root, "http://localhost", lambda *args: ({"id": 1, "file_id": 1, "version": 1}, {})
    )
    assert config["steps"][0]["options"]["poll"] == {
        "max_attempts": 4,
        "interval_milliseconds": 5,
        "timeout_seconds": 6,
    }


def test_poll_preview_uses_only_final_response(monkeypatch):
    from app.service.task_execution import preview

    step = poll_step()
    step.update(
        interface={"method": "GET", "path": "/status", "parameters": [], "request_body": None},
        document={},
        bindings=[],
        headers=[],
    )
    config = {
        "base_url": "http://localhost",
        "steps": [step],
        "global_parameters": [
            {
                "name": "done",
                "source_type": "rest",
                "value_type": "boolean",
                "sensitive": False,
                "rest_source": {"step_id": 1, "extract_expression": "done"},
            }
        ],
    }
    calls = iter([response(), response(True)])
    send = MagicMock(side_effect=lambda *args: next(calls))
    monkeypatch.setattr("app.service.task_execution.request_http", send)
    validate_config(config)
    assert preview(config, "done")["value"] is True
    assert send.call_count == 2


def test_poll_time_budget_limits_attempts(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.service.task_execution.time.monotonic", lambda: clock[0])
    calls = []

    def send(request, timeout):
        calls.append(timeout)
        clock[0] += 2
        return response()

    monkeypatch.setattr("app.service.task_execution.request_http", send)
    result = request_with_poll({}, poll_step(timeout_seconds=1), {}, {})
    assert result["error_code"] == "POLL_EXHAUSTED"
    assert calls == [1]
