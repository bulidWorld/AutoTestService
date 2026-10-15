import json

import pytest

from app.api.business import validate_value
from app.service.assertions import evaluate_assertion, validate_assertion
from app.service.errors import BusinessError
from app.service.task_execution import apply_assertion


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
        "def assert_response(responseBody, globalParam):\n    import os\n    return True",
        "def assert_response(responseBody, globalParam):\n    return responseBody.__class__",
        "raise ValueError('must never execute during validation')",
        "@print('must never execute')\ndef assert_response(responseBody, globalParam):\n    return True",
        "def assert_response(responseBody, globalParam={}):\n    return True",
        "def assert_response(responseBody, globalParam):\n    yield True",
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
    code = f"def assert_response(responseBody, globalParam):\n    return {expression}\n"
    assert evaluate_assertion(code, {"items": [{"id": 1}]}, {"T1": 1}) == expected


def test_python_helpers_and_input_mutation_are_isolated():
    code = """def matching(items, expected):
    return all(item["id"] == expected for item in items)
def assert_response(responseBody, globalParam):
    result = matching(responseBody["items"], globalParam["T1"])
    globalParam["T1"] = 999
    return result
"""
    values = {"T1": 1}
    assert validate_assertion(code, "code") == code
    assert evaluate_assertion(code, {"items": [{"id": 1}]}, values) is None
    assert values == {"T1": 1}


def test_nonterminating_assertion_is_stopped():
    assert (
        evaluate_assertion(
            "def assert_response(responseBody, globalParam):\n    while True:\n        pass\n",
            {},
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
    step = {"assertion_code": "def assert_response(responseBody, globalParam):\n    return True"}
    assert apply_assertion(step, response, {})["error_code"] is None


def test_transport_errors_skip_assertion_and_non_json_fails(monkeypatch):
    def unexpected(*args):
        pytest.fail("assertion should not execute")

    monkeypatch.setattr("app.service.task_execution.evaluate_assertion", unexpected)
    step = {"assertion_code": "def assert_response(responseBody, globalParam):\n    return True"}
    for code in ("CONNECTION_ERROR", "TIMEOUT"):
        response = {"error_code": code}
        assert apply_assertion(step, response, {}) is response
    response = {"error_code": None, "response_body": "not-json", "response_body_encoding": "utf-8"}
    assert apply_assertion(step, response, {})["error_code"] == "RESPONSE_NOT_JSON"
