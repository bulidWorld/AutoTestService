import http.client
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.business import validate_value
from app.service.assertions import evaluate_assertion, validate_assertion
from app.service.errors import BusinessError
from app.service.task_execution import apply_assertion, request_http


def test_openapi_nullable_validation_keeps_other_constraints():
    schema = {
        "type": "object",
        "required": ["code"],
        "additionalProperties": False,
        "properties": {"code": {"type": "string", "nullable": True, "maxLength": 3}},
    }
    validate_value({"code": None}, schema, schema, "body")
    for value in ({}, {"code": 1}, {"code": "long"}, {"code": None, "extra": 1}):
        with pytest.raises(BusinessError):
            validate_value(value, schema, schema, "body")


@pytest.mark.parametrize("code", [None, "", " \n\t"])
def test_empty_assertion(code):
    assert validate_assertion(code, "body.steps.0.assertion_code") is None


@pytest.mark.parametrize(
    "code",
    [
        "def assert(responseBody, globalParam):\n    return True",
        "def assert_response(body, values):\n    return True",
        "def assert_response(responseBody, globalParam, extra):\n    return True",
        "def assert_response(responseBody, globalParam, statusCode, headers):\n    import os\n    return True",
        "def assert_response(responseBody, globalParam, statusCode, headers):\n    return responseBody.__class__",
        "raise ValueError('must never execute during validation')",
        "@print('must never execute')\ndef assert_response(responseBody, globalParam, statusCode, headers):\n    return True",
        "def assert_response(responseBody, globalParam, statusCode, headers={}):\n    return True",
        "def assert_response(responseBody, globalParam, statusCode, headers):\n    yield True",
    ],
)
def test_invalid_assertion_is_located_without_execution(code):
    with pytest.raises(BusinessError) as error:
        validate_assertion(code, "body.steps.1.assertion_code")
    assert error.value.status == 400
    assert error.value.payload["issues"][0]["location"] == "body.steps.1.assertion_code"


@pytest.mark.parametrize(
    "expression,expected",
    [
        ('responseBody["items"][0]["id"] == globalParam["T1"]', None),
        ("False", "ASSERTION_FAILED"),
        ("1", "ASSERTION_RETURN_TYPE_ERROR"),
        ("None", "ASSERTION_RETURN_TYPE_ERROR"),
        ('responseBody["missing"]', "ASSERTION_EXECUTION_ERROR"),
        ("open('/etc/passwd').read()", "ASSERTION_EXECUTION_ERROR"),
    ],
)
def test_assertion_verdicts(expression, expected):
    code = f"def assert_response(responseBody, globalParam, statusCode, headers):\n    return {expression}\n"
    assert evaluate_assertion(code, {"items": [{"id": 1}]}, {"T1": 1}, 200, {}) == expected


def test_python_helpers_and_input_mutation_are_isolated():
    code = """def matching(items, expected):
    return all(item["id"] == expected for item in items)
def assert_response(responseBody, globalParam, statusCode, headers):
    result = matching(responseBody["items"], globalParam["T1"])
    globalParam["T1"] = 999
    return result
"""
    values = {"T1": 1}
    assert validate_assertion(code, "code") == code
    assert evaluate_assertion(code, {"items": [{"id": 1}]}, values, 200, {}) is None
    assert values == {"T1": 1}


def test_nonterminating_assertion_is_stopped():
    assert (
        evaluate_assertion(
            "def assert_response(responseBody, globalParam, statusCode, headers):\n    while True:\n        pass\n",
            {},
            {},
            200,
            {},
        )
        == "ASSERTION_EXECUTION_ERROR"
    )


@pytest.mark.parametrize("body", [None, True, 3, "text", [1], {"id": 1}])
def test_assertion_accepts_all_json_response_types(body):
    response = {
        "response_body": json.dumps(body),
        "response_body_encoding": "utf-8",
        "error_code": "HTTP_STATUS_ERROR",
        "error_message": "HTTP 状态码不为 2xx",
    }
    step = {
        "assertion_code": "def assert_response(responseBody, globalParam, statusCode, headers):\n    return True"
    }
    assert apply_assertion(step, response, {})["error_code"] is None


def test_transport_errors_skip_assertion(monkeypatch):
    def unexpected(*args):
        pytest.fail("assertion should not execute")

    monkeypatch.setattr("app.service.task_execution.evaluate_assertion", unexpected)
    step = {
        "assertion_code": "def assert_response(responseBody, globalParam, statusCode, headers):\n    return True"
    }
    for code in ("CONNECTION_ERROR", "TIMEOUT"):
        response = {"error_code": code}
        assert apply_assertion(step, response, {}) is response


@pytest.mark.parametrize("status", [200, 400, 401])
@pytest.mark.parametrize("actual_status", [200, 400, 401])
def test_http_status_is_independent_of_business_code(status, actual_status):
    code = (
        "def assert_response(responseBody, globalParam, statusCode, headers):\n"
        f"    return statusCode == {status} and responseBody['code'] == 200\n"
    )
    response = {
        "response_status": actual_status,
        "response_headers": [],
        "response_body": '{"code": 200}',
        "response_body_encoding": "utf-8",
        "error_code": None if actual_status == 200 else "HTTP_STATUS_ERROR",
    }
    result = apply_assertion({"assertion_code": code}, response, {})
    assert result["error_code"] == (None if status == actual_status else "ASSERTION_FAILED")
    assert result["response_status"] == actual_status
    assert response["error_code"] == (None if actual_status == 200 else "HTTP_STATUS_ERROR")


@pytest.mark.parametrize(
    "body,encoding", [("", "utf-8"), ("unauthorized", "utf-8"), ("/w==", "base64")]
)
def test_http_assertion_handles_non_json_and_preserves_repeated_headers(body, encoding):
    response = {
        "response_status": 401,
        "response_headers": [
            {"name": "WWW-Authenticate", "value": "Bearer"},
            {"name": "Set-Cookie", "value": "a=1"},
            {"name": "set-cookie", "value": "b=2"},
        ],
        "response_body": body,
        "response_body_encoding": encoding,
        "error_code": "HTTP_STATUS_ERROR",
    }
    code = """def assert_response(responseBody, globalParam, statusCode, headers):
    result = (statusCode == 401 and responseBody is None
              and headers.get("www-authenticate") == ["Bearer"]
              and headers.get("set-cookie") == ["a=1", "b=2"]
              and "missing" not in headers)
    headers["set-cookie"].append("changed")
    return result
"""
    assert apply_assertion({"assertion_code": code}, response, {})["error_code"] is None
    assert len(response["response_headers"]) == 3
    assert response["response_headers"][-1]["value"] == "b=2"


@pytest.mark.parametrize("code", ["CONNECTION_ERROR", "TIMEOUT"])
def test_http_assertion_cannot_override_transport_errors(code, monkeypatch):
    def unexpected(*args):
        pytest.fail("assertion should not execute")

    monkeypatch.setattr("app.service.task_execution.evaluate_assertion", unexpected)
    step = {
        "assertion_code": (
            "def assert_response(responseBody, globalParam, statusCode, headers):\n    return True"
        )
    }
    response = {"error_code": code}
    assert apply_assertion(step, response, {}) is response


@pytest.mark.parametrize(
    "arguments",
    [
        "responseBody, globalParam",
        "responseBody, globalParam, statusCode",
        "responseBody, globalParam, headers, statusCode",
        "responseBody, globalParam, statusCode, headers, extra",
    ],
)
def test_invalid_http_assertion_signatures(arguments):
    with pytest.raises(BusinessError):
        validate_assertion(f"def assert_response({arguments}):\n    return True", "code")


@pytest.mark.parametrize("status", [204, 401])
def test_http_empty_response_can_be_asserted(status, monkeypatch):
    # Parse HTTP bytes with the production parser, without binding a local socket.
    wire = (
        f"HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nX-Check: first\r\nx-check: second\r\n\r\n"
    ).encode("ascii")
    incoming = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *args: BytesIO(wire)))
    incoming.begin()
    client = Mock()
    client.getresponse.return_value = incoming
    monkeypatch.setattr(
        "app.service.task_execution.http.client.HTTPConnection", lambda *a, **k: client
    )
    request = {
        "method": "GET",
        "actual_url": "http://localhost/",
        "request_headers": [],
        "request_body": None,
    }
    response = request_http(request, 2)
    code = (
        "def assert_response(responseBody, globalParam, statusCode, headers):\n"
        f"    return statusCode == {status} and responseBody is None "
        "and headers['x-check'] == ['first', 'second']"
    )
    assert response["response_status"] == status
    assert apply_assertion({"assertion_code": code}, response, {})["error_code"] is None
    client.close.assert_called_once()
