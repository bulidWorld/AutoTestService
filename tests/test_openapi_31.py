"""OpenAPI 3.1 import and request validation regressions."""

import pytest
import yaml

from app.service.errors import BusinessError
from app.service.openapi_import import parse_upload
from app.service.task_config import build_request, validate_json


def document(schema):
    return {
        "openapi": "3.1.0",
        "info": {"title": "Auth", "version": "1"},
        "paths": {
            "/login": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
        "components": {"schemas": {"Count": {"type": "integer", "minimum": 1}}},
    }


def parse(doc):
    return parse_upload("auth.yaml", yaml.safe_dump(doc).encode())


def test_import_and_execute_31_keywords():
    schema = {
        "type": "object",
        "required": ["token_type", "count", "label"],
        "properties": {
            "token_type": {"type": "string", "const": "Bearer"},
            "count": {"$ref": "#/components/schemas/Count", "exclusiveMinimum": 2},
            "label": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    parsed = parse(document(schema))
    interface = parsed["interfaces"][0]
    count_schema = interface["request_body"]["schema"]["properties"]["count"]
    for value in (0, 2):
        with pytest.raises(BusinessError):
            validate_json(value, count_schema, parsed["document"], "count")
    validate_json(3, count_schema, parsed["document"], "count")
    step = {
        "interface": interface,
        "document": parsed["document"],
        "bindings": [
            {
                "location": "body",
                "name": "",
                "binding": {
                    "kind": "literal",
                    "value": {"token_type": "Bearer", "count": 3, "label": None},
                },
            }
        ],
    }
    assert build_request({"base_url": "http://localhost"}, step)["request_body"]
    for field, value in [("token_type", "Basic"), ("count", 2), ("count", 0)]:
        step["bindings"][0]["binding"]["value"][field] = value
        with pytest.raises(BusinessError):
            build_request({"base_url": "http://localhost"}, step)
        step["bindings"][0]["binding"]["value"].update(token_type="Bearer", count=3)


@pytest.mark.parametrize(
    "schema,value",
    [
        ({"const": "Bearer"}, "Basic"),
        ({"type": "number", "exclusiveMinimum": 0}, 0),
        ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1),
        ({"not": {"type": "string"}}, "text"),
        (False, "text"),
    ],
)
def test_31_validation_enforces_keywords(schema, value):
    with pytest.raises(BusinessError):
        validate_json(value, schema, document(schema), "body")


def test_31_nullable_type_array_with_global_binding():
    parsed = parse(
        document({"type": "object", "properties": {"name": {"type": ["string", "null"]}}})
    )
    step = {
        "interface": parsed["interfaces"][0],
        "document": parsed["document"],
        "bindings": [
            {
                "location": "body",
                "name": "/name",
                "binding": {"kind": "global", "parameter_name": "name"},
            }
        ],
    }
    request = build_request(
        {"base_url": "http://localhost"},
        step,
        declarations={"name": {"source_type": "fixed", "value_type": "string", "value": "Alice"}},
    )
    assert request["request_body"] == '{"name":"Alice"}'


def test_31_external_references_still_rejected():
    with pytest.raises(BusinessError) as exc:
        parse(document({"$ref": "https://example.com/schema.json"}))
    assert exc.value.payload["code"] == "UNSUPPORTED_SCHEMA"


def test_unsupported_version_rejected():
    doc = document({})
    doc["openapi"] = "3.2.0"
    with pytest.raises(BusinessError) as exc:
        parse(doc)
    assert exc.value.payload["issues"][0]["location"] == "/openapi"
