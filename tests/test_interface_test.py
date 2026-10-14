"""Single interface asset testing, using isolated PostgreSQL and a local HTTP service."""

import socket

import pytest
import test_task_business as task_helpers
import yaml
from fastapi.testclient import TestClient
from jsonschema import RefResolver
from openapi_schema_validator import OAS30Validator
from openapi_spec_validator import validate

from app.api.contract import load_contract
from app.config.settings import ROOT
from app.main import create_app

api = task_helpers.api
database = task_helpers.database
server = task_helpers.server
asset = task_helpers.asset

SPEC = load_contract(ROOT / "docs/rest-project.yaml")


def check(response, status=200, name="InterfaceTestResult"):
    assert response.status_code == status, response.text
    OAS30Validator(
        SPEC["components"]["schemas"][name], resolver=RefResolver.from_schema(SPEC)
    ).validate(response.json())
    return response.json()


def test_contract_and_asset_page(monkeypatch):
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda _: None)
    validate(SPEC)
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "单次测试" in response.text and "项目与接口管理" in response.text
        script = client.get("/static/asset-test.js")
        assert script.status_code == 200
        assert "submitTest" in script.text and "/test" in script.text
        assert (
            client.post(
                "/api/v1/projects/1/yaml-files/1/interfaces/1/test",
                json={"base_url": "http://localhost", "parameters": [], "method": "DELETE"},
            ).status_code
            == 400
        )


def test_single_json_request_preserves_encoding_and_types(api, server, database):
    p, fid, steps, _ = asset(api, server)
    iid = steps["/orders/{id}"]["interface_id"]
    payload = {
        "base_url": server["url"],
        "parameters": [
            {"location": "path", "name": "id", "value": 10},
            {"location": "query", "name": "id", "value": [1, 2]},
            {"location": "header", "name": "X-Request-Label", "value": "demo"},
        ],
        "request_body": {"data": {"a": 1, "enabled": True, "items": [1, 2]}},
    }
    result = check(api.post(p + f"/yaml-files/{fid}/interfaces/{iid}/test", json=payload))
    assert result["status"] == "succeeded" and result["response_status"] == 204
    assert result["actual_url"] == server["url"] + "/orders/10?id=1&id=2"
    assert len(server["calls"]) == 1
    assert server["calls"][0]["body"] == payload["request_body"]
    assert server["calls"][0]["headers"]["X-Request-Label"] == "demo"
    with database() as db:
        for table in ["tasks", "task_runs", "task_requests"]:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad_parameters,bad_body",
    [
        ([], {"data": {}}),
        ([{"location": "path", "name": "id", "value": "bad"}], {"data": {}}),
        ([{"location": "path", "name": "id", "value": 1}], {}),
        (
            [
                {"location": "path", "name": "id", "value": 1},
                {"location": "query", "name": "id", "value": {"a": 1}},
            ],
            {"data": {}},
        ),
        (
            [
                {"location": "path", "name": "id", "value": 1},
                {"location": "header", "name": "Host", "value": "evil"},
            ],
            {"data": {}},
        ),
    ],
)
def test_invalid_inputs_never_send_http(api, server, bad_parameters, bad_body):
    p, fid, steps, _ = asset(api, server)
    iid = steps["/orders/{id}"]["interface_id"]
    result = check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={
                "base_url": server["url"],
                "parameters": bad_parameters,
                "request_body": bad_body,
            },
        ),
        400,
        "Error",
    )
    assert result["issues"] and server["calls"] == []


@pytest.mark.parametrize(
    "remote_path,code",
    [("/fail", "HTTP_STATUS_ERROR"), ("/redirect", "HTTP_STATUS_ERROR"), ("/slow", "TIMEOUT")],
)
def test_remote_failures_return_a_single_attempt_result(api, server, remote_path, code):
    p, fid, steps, _ = asset(api, server)
    iid = steps[remote_path]["interface_id"]
    result = check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={"base_url": server["url"], "timeout_seconds": 1, "parameters": []},
        )
    )
    assert result["status"] == "failed" and result["error_code"] == code
    assert len(server["calls"]) == 1


def test_connection_error_returns_no_http_status(api, server):
    p, fid, steps, _ = asset(api, server)
    iid = steps["/ok"]["interface_id"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    result = check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={"base_url": f"http://127.0.0.1:{port}", "parameters": []},
        )
    )
    assert result["error_code"] == "CONNECTION_ERROR" and result["response_status"] is None
    assert server["calls"] == []


def test_sensitive_header_response_echo_and_log_redaction(api, server):
    from app.config.settings import absolute, load_settings

    p, fid, steps, _ = asset(api, server)
    iid = steps["/init"]["interface_id"]
    response = api.post(
        p + f"/yaml-files/{fid}/interfaces/{iid}/test",
        json={
            "base_url": server["url"],
            "parameters": [
                {"location": "header", "name": "Authorization", "value": "PRIVATE_TOKEN"}
            ],
        },
    )
    result = check(response)
    assert server["calls"][0]["headers"]["Authorization"] == "PRIVATE_TOKEN"
    assert result["redacted"] and "PRIVATE_" not in response.text
    assert result["request_headers"][0]["redacted"]
    logs = absolute(load_settings()["logging"]["directory"])
    for path in logs.glob("*.log"):
        assert "PRIVATE_" not in path.read_text()


def test_body_sensitive_masks_request_and_response(api, server):
    p, fid, steps, _ = asset(api, server)
    iid = steps["/orders/{id}"]["interface_id"]
    result = check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={
                "base_url": server["url"],
                "parameters": [{"location": "path", "name": "id", "value": 1}],
                "request_body": {"data": {"credential": "PRIVATE_BODY"}},
                "body_sensitive": True,
            },
        )
    )
    assert result["request_body"] == "***" and result["response_body"] == "***"
    assert server["calls"][0]["body"]["data"]["credential"] == "PRIVATE_BODY"


def test_interface_ownership_and_selected_historical_version(api, server):
    p, fid, steps, _ = asset(api, server)
    iid = steps["/ok"]["interface_id"]
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "changed", "version": "2"},
        "paths": {"/fail": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    response = api.post(
        p + f"/yaml-files/{fid}/versions",
        files={"file": ("v2.yaml", yaml.safe_dump(doc).encode(), "application/yaml")},
    )
    assert response.status_code == 201
    result = check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={"base_url": server["url"], "parameters": []},
        )
    )
    assert result["version"] == 1 and server["calls"][-1]["path"] == "/prefix/ok"
    other = api.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    check(
        api.post(
            f"/api/v1/projects/{other}/yaml-files/{fid}/interfaces/{iid}/test",
            json={"base_url": server["url"], "parameters": []},
        ),
        404,
        "Error",
    )
    assert len(server["calls"]) == 1


def test_multipart_is_rejected_before_network(api, server):
    p, _, _, _ = asset(api, server)
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "upload", "version": "1"},
        "paths": {
            "/upload": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"file": {"type": "string", "format": "binary"}},
                                    "required": ["file"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    imported = api.post(
        p + "/yaml-files",
        data={"name": "upload"},
        files={"file": ("upload.yaml", yaml.safe_dump(doc).encode(), "application/yaml")},
    )
    assert imported.status_code == 201
    fid = imported.json()["file"]["id"]
    iid = api.get(p + f"/yaml-files/{fid}/interfaces").json()["items"][0]["id"]
    check(
        api.post(
            p + f"/yaml-files/{fid}/interfaces/{iid}/test",
            json={"base_url": server["url"], "parameters": [], "request_body": {"file": "demo"}},
        ),
        400,
        "Error",
    )
    assert server["calls"] == []
