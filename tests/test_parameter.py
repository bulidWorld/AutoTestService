"""Supplied-response extraction needs neither database access nor outbound HTTP."""

import json

import pytest
from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from app.api.contract import load_contract
from app.config.settings import ROOT
from app.main import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text("logging:\n  directory: " + str(tmp_path / "logs") + "\n")
    monkeypatch.setenv("APP_CONFIG", str(settings))
    monkeypatch.setattr("app.service.task_execution.Dispatcher.start", lambda _: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not connect to a database or send HTTP requests")

    monkeypatch.setattr("app.repository.projects.connect", forbidden)
    monkeypatch.setattr("app.service.task_execution.request_http", forbidden)
    with TestClient(create_app()) as test_client:
        yield test_client


def inputs(value=1):
    return {
        "parameter_name": "key1",
        "value_type": "integer",
        "extract_expression": "data.items[0].a",
        "response": {"response_body": json.dumps({"data": {"items": [{"a": value}]}})},
    }


@pytest.mark.parametrize(
    "kind,value",
    [
        ("integer", 1),
        ("number", 1.25),
        ("string", "demo"),
        ("boolean", False),
        ("object", {"a": 1}),
        ("array", [1, 2]),
    ],
)
def test_extract_typed_value(client, kind, value):
    payload = inputs(value)
    payload["value_type"] = kind
    payload["parameter_name"] = " key1 "
    response = client.post("/api/v1/parameters/rest/extract", json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "parameter_name": "key1",
        "value_type": kind,
        "extract_expression": payload["extract_expression"],
        "value": value,
    }


@pytest.mark.parametrize(
    "changes,status,code",
    [
        ({"extract_expression": "data..a"}, 400, "INVALID_REQUEST"),
        ({"extract_expression": '__import__("os")'}, 400, "INVALID_REQUEST"),
        ({"extract_expression": "data.items[-1]"}, 400, "INVALID_REQUEST"),
        ({"extract_expression": "data.missing"}, 422, "EXTRACTION_NOT_FOUND"),
        ({"parameter_name": "   "}, 400, "INVALID_REQUEST"),
        ({"value_type": "unknown"}, 400, "INVALID_REQUEST"),
        ({"response": {"response_body": "PRIVATE_INVALID_JSON"}}, 422, "RESPONSE_NOT_JSON"),
        ({"response": {"response_body": '{"a":NaN}'}}, 422, "RESPONSE_NOT_JSON"),
        (
            {"response": {"response_body": "e30=", "response_body_encoding": "base64"}},
            422,
            "RESPONSE_NOT_JSON",
        ),
    ],
)
def test_extract_errors(client, changes, status, code):
    response = client.post("/api/v1/parameters/rest/extract", json=inputs() | changes)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["issues"]
    assert "PRIVATE_INVALID_JSON" not in response.text


@pytest.mark.parametrize("value", [True, None, "PRIVATE_VALUE"])
def test_extract_type_mismatch(client, value):
    response = client.post("/api/v1/parameters/rest/extract", json=inputs(value))
    assert response.status_code == 422
    assert response.json()["code"] == "EXTRACTION_TYPE_ERROR"
    assert "PRIVATE_VALUE" not in response.text


def test_split_contracts_are_bundled_and_routes_registered(client):
    for path in (ROOT / "docs").glob("rest-*.yaml"):
        document = load_contract(path)
        validate(document)
        assert ".yaml#/components/" not in json.dumps(document)
    document = client.get("/openapi.json").json()
    validate(document)
    assert "/api/v1/parameters/rest/extract" in document["paths"]
    assert "/api/v1/projects/{project_id}/tasks/{task_id}/parameters/preview" in document["paths"]
    assert "/api/v1/projects/{project_id}/tasks" in document["paths"]
