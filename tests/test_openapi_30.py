"""OpenAPI 3.0 combination schema import and validation regressions."""

import pytest
import yaml

from app.service.errors import BusinessError
from app.service.openapi_import import parse_upload
from app.service.task_config import build_request, validate_json


def document(schema, responses=None, components=None):
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Auth", "version": "1"},
        "paths": {
            "/login": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": schema}},
                    },
                    "responses": responses or {"200": {"description": "OK"}},
                }
            }
        },
    }
    if components is not None:
        doc["components"] = components
    return doc


def parse(doc):
    return parse_upload("auth.yaml", yaml.safe_dump(doc).encode())


def test_30_allof_response_schema_imports():
    """FastAPI 风格 EmptyResponse（allOf 响应组件）可导入。"""
    doc = document(
        {},
        responses={"200": {"$ref": "#/components/responses/EmptyResponse"}},
        components={
            "responses": {
                "EmptyResponse": {
                    "description": "无内容",
                    "content": {
                        "application/json": {
                            "schema": {"allOf": [{"$ref": "#/components/schemas/Empty"}]}
                        }
                    },
                }
            },
            "schemas": {"Empty": {"type": "object"}},
        },
    )
    parsed = parse(doc)
    assert parsed["interfaces"][0]["responses"]["200"]


@pytest.mark.parametrize(
    "schema,valid,invalid",
    [
        (
            {
                "allOf": [
                    {"type": "object"},
                    {"properties": {"name": {"type": "string"}}},
                ]
            },
            {"name": "x"},
            {"name": 1},
        ),
        (
            {"oneOf": [{"type": "integer"}, {"type": "number"}]},
            1.5,
            1,
        ),
        (
            {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            1,
            1.5,
        ),
        ({"not": {"type": "string"}}, 1, "x"),
    ],
)
def test_30_combination_schemas_validate(schema, valid, invalid):
    parsed = parse(document(schema))
    body_schema = parsed["interfaces"][0]["request_body"]["schema"]
    validate_json(valid, body_schema, parsed["document"], "body")
    with pytest.raises(BusinessError):
        validate_json(invalid, body_schema, parsed["document"], "body")


def test_30_allof_body_whole_binding():
    """allOf 请求体保留原结构，内部引用展开，可整体绑定。"""
    schema = {
        "allOf": [
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
            {"$ref": "#/components/schemas/Count"},
        ]
    }
    parsed = parse(
        document(
            schema,
            components={
                "schemas": {
                    "Count": {
                        "type": "object",
                        "required": ["count"],
                        "properties": {"count": {"type": "integer"}},
                    }
                }
            },
        )
    )
    body_schema = parsed["interfaces"][0]["request_body"]["schema"]
    assert body_schema["allOf"][1]["properties"]["count"]["type"] == "integer"
    step = {
        "interface": parsed["interfaces"][0],
        "document": parsed["document"],
        "bindings": [
            {
                "location": "body",
                "name": "",
                "binding": {
                    "kind": "literal",
                    "value": {"name": "a", "count": 2},
                },
            }
        ],
    }
    assert build_request({"base_url": "http://localhost"}, step)["request_body"]
    step["bindings"][0]["binding"]["value"]["count"] = "x"
    with pytest.raises(BusinessError):
        build_request({"base_url": "http://localhost"}, step)


def test_30_recursive_allof_imports():
    """递归 allOf 在环边界保留引用，导入不失控。"""
    parsed = parse(
        document(
            {},
            components={
                "schemas": {
                    "Node": {
                        "allOf": [
                            {"type": "object"},
                            {"$ref": "#/components/schemas/Node"},
                        ]
                    }
                }
            },
        )
    )
    assert parsed["document"]["components"]["schemas"]["Node"]
