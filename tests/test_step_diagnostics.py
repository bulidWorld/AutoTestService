"""Step correlation survives concurrent execution; diagnostics preserve no input secrets."""

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.config.logging import JsonFormatter, step_context
from app.service import task_execution
from app.service.assertions import evaluate_assertion


def test_rest_request_propagates_backend_uuid(monkeypatch):
    client = Mock()
    client.getresponse.return_value.status = 200
    client.getresponse.return_value.getheaders.return_value = []
    client.getresponse.return_value.read.return_value = b"{}"
    monkeypatch.setattr(task_execution.http.client, "HTTPConnection", lambda *a, **k: client)
    identifier = str(uuid4())
    token = step_context.set({"run_uuid": identifier})
    try:
        task_execution.request_http(
            {
                "actual_url": "http://localhost/",
                "method": "GET",
                "request_body": None,
                "request_headers": [{"name": "x-task-step-run-uuid", "value": "custom"}],
            },
            2,
        )
    finally:
        step_context.reset(token)
    client.putheader.assert_called_once_with("X-Task-Step-Run-UUID", identifier)


def test_incoming_uuid_correlates_invalid_json_log(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from app.main import create_app

    config = tmp_path / "settings.yaml"
    directory = tmp_path / "logs"
    config.write_text(f"logging:\n  directory: {directory}\n")
    monkeypatch.setenv("APP_CONFIG", str(config))
    application = create_app()
    identifier = str(uuid4())

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            for header, expected in ((identifier, identifier), ("invalid", None), (None, None)):
                headers = {"Content-Type": "application/json"}
                if header:
                    headers["X-Task-Step-Run-UUID"] = header
                response = await client.post(
                    "/api/v1/projects/1/tasks", content="{", headers=headers
                )
                assert response.status_code == 400
                records = [
                    json.loads(line) for line in (directory / "error.log").read_text().splitlines()
                ]
                record = next(
                    r for r in records if r["request_id"] == response.headers["X-Request-ID"]
                )
                assert record.get("run_uuid") == expected
                assert record["detail"] == "请求必须为有效 JSON"
                assert step_context.get() is None

    asyncio.run(check())


def test_assertion_reports_helper_stack_without_exception_values():
    diagnostics = {}
    code = """def helper(body):
    raise ValueError(body["secret"])
def assert_response(responseBody, globalParam, statusCode, headers):
    return helper(responseBody)
"""
    assert (
        evaluate_assertion(code, {"secret": "PRIVATE_SECRET"}, {}, 200, {}, diagnostics)
        == "ASSERTION_EXECUTION_ERROR"
    )
    assert diagnostics["exception"] == "ValueError"
    assert [(f["function"], f["line"]) for f in diagnostics["stack"]] == [
        ("assert_response", 4),
        ("helper", 2),
    ]
    assert "PRIVATE_SECRET" not in json.dumps(diagnostics)


def test_assertion_distinguishes_timeout_and_process_exit(monkeypatch):
    code = "def assert_response(responseBody, globalParam, statusCode, headers):\n    return True"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 3)

    monkeypatch.setattr("app.service.assertions.subprocess.run", timeout)
    detail = {}
    assert evaluate_assertion(code, {}, {}, 200, {}, detail) == "ASSERTION_EXECUTION_ERROR"
    assert "3 秒" in detail["reason"]
    monkeypatch.setattr(
        "app.service.assertions.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=-9)
    )
    detail = {}
    assert evaluate_assertion(code, {}, {}, 200, {}, detail) == "ASSERTION_EXECUTION_ERROR"
    assert detail["returncode"] == -9


def test_parallel_step_context_isolated_and_restored(monkeypatch):
    identifiers = {1: 101, 2: 102}
    uuids = {1: uuid4(), 2: uuid4()}
    barrier = Barrier(2)
    output = []
    formatter = JsonFormatter("test")

    class Capture(logging.Handler):
        def emit(self, record):
            output.append(json.loads(formatter.format(record)))

    @contextmanager
    def connection():
        class DB:
            def execute(self, sql, args):
                return SimpleNamespace(
                    fetchone=lambda: {"id": identifiers[args[1]], "run_uuid": uuids[args[1]]}
                )

        yield DB()

    monkeypatch.setattr(task_execution, "connection", connection)

    def pipeline(self, config, step, values, abort=None):
        barrier.wait(timeout=3)
        for stage in ("rest", "extraction", "assertion"):
            task_execution.LOGGER.warning("stage", extra={"stage": stage})
        return {}, None

    monkeypatch.setattr(task_execution.RunExecutor, "_leaf", pipeline)
    handler = Capture()
    task_execution.LOGGER.addHandler(handler)
    token = step_context.set({"run_id": "outer"})
    try:
        executor = task_execution.RunExecutor(20)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(executor.leaf, {}, {"id": i}, {}) for i in (1, 2)]
            for future in futures:
                assert future.result() == ({}, None)
        assert step_context.get() == {"run_id": "outer"}
    finally:
        step_context.reset(token)
        task_execution.LOGGER.removeHandler(handler)
    stages = [r for r in output if r["message"] == "stage"]
    assert len(stages) == 6
    for row in stages:
        assert row["run_step_id"] == identifiers[row["step_id"]]
        assert row["run_id"] == 20
        assert row["run_uuid"] == str(uuids[row["step_id"]])


def test_assertion_failure_message_contains_line_and_type():
    result = task_execution.apply_assertion(
        {
            "assertion_code": "def assert_response(responseBody, globalParam, statusCode, headers):\n    return responseBody['missing']"
        },
        {
            "error_code": None,
            "response_status": 200,
            "response_body": "{}",
            "response_body_encoding": "utf-8",
        },
        {},
    )
    assert "KeyError" in result["error_message"]
    assert "<assertion>:2" in result["error_message"]


def test_failed_leaf_persists_step_and_stage_context(monkeypatch):
    run_step_id = 101
    run_uuid = uuid4()
    updates = []

    @contextmanager
    def connection():
        class DB:
            def execute(self, sql, args):
                updates.append((sql, args))
                if sql.startswith("SELECT id,run_uuid FROM task_run_steps"):
                    row = {"id": run_step_id, "run_uuid": run_uuid}
                elif sql.startswith("SELECT * FROM task_run_steps"):
                    row = {"id": 10, "status": "pending"}
                else:
                    row = None
                return SimpleNamespace(fetchone=lambda: row)

        yield DB()

    monkeypatch.setattr(task_execution, "connection", connection)
    executor = task_execution.RunExecutor(20)
    executor.sources = {}
    _, error = executor.leaf(
        {},
        {
            "id": 8,
            "step_type": "wait",
            "execution_scope": "main",
            "options": {"condition": {"code": "bad"}},
        },
        {},
    )
    assert error is not None
    assert "run_id=20" in error[1]
    assert f"run_uuid={run_uuid}" in error[1]
    assert f"run_step_id={run_step_id}" in error[1]
    assert "步骤 8" in error[1]
    assert "阶段=" in error[1]
    assert any(error[1] in args for sql, args in updates if sql.startswith("UPDATE task_run_steps"))
    assert step_context.get() is None
