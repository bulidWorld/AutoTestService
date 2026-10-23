"""Task UUID built-ins and expression validation."""

from uuid import UUID

import pytest
from lxml import etree

from app.service.errors import BusinessError
from app.service.task_execution import initial_parameter_value
from app.service.task_expressions import ExpressionContext, expression_tree


def test_uuid_expression_and_arity():
    context = ExpressionContext({}, "Asia/Shanghai")
    tree = expression_tree("generateUUID()", "expression")
    values = [context.evaluate(tree.body) for _ in range(2)]
    assert all(str(UUID(value, version=4)) == value for value in values)
    assert all(UUID(value).version == 4 for value in values)
    assert values[0] != values[1]
    with pytest.raises(BusinessError):
        expression_tree('generateUUID("unexpected")', "expression")


def test_initialization_generates_fresh_values_without_mutating_config():
    declaration = {"source_type": "function", "value_type": "string", "value": "generateUUID()"}
    first, second = (initial_parameter_value(declaration) for _ in range(2))
    assert UUID(first).version == UUID(second).version == 4
    assert first != second
    assert declaration["value"] == "generateUUID()"
    assert initial_parameter_value(
        {"source_type": "fixed", "value_type": "object", "value": {"id": "generateUUID()"}}
    ) == {"id": "generateUUID()"}
    assert (
        initial_parameter_value(
            {"source_type": "fixed", "value_type": "string", "value": " generateUUID()"}
        )
        == " generateUUID()"
    )


def test_uuid_global_parameter_xsd():
    schema = etree.XMLSchema(etree.parse("docs/task.xsd"))
    schema.assertValid(
        etree.fromstring(b"""<task name="uuid">
      <parameters><parameter type="function" valueType="string">
        <name>task_uuid</name><value>generateUUID()</value>
      </parameter></parameters>
      <taskStep order="1"><wait milliseconds="1"/></taskStep>
    </task>""")
    )


def test_fixed_function_text_is_literal():
    assert (
        initial_parameter_value(
            {"source_type": "fixed", "value_type": "string", "value": "generateUUID()"}
        )
        == "generateUUID()"
    )


@pytest.mark.parametrize(
    "value,value_type",
    [
        ("unknown()", "string"),
        ('generateUUID("x")', "string"),
        ("generateUUID()", "integer"),
        ("__import__('os')", "string"),
        (" generateUUID()", "string"),
    ],
)
def test_invalid_function_declarations(value, value_type):
    from app.service.task_config import validate_config

    config = {
        "base_url": "http://localhost",
        "global_parameters": [
            {"name": "id", "source_type": "function", "value_type": value_type, "value": value}
        ],
        "steps": [],
    }
    with pytest.raises(BusinessError):
        validate_config(config)


def test_import_preserves_explicit_function_source():
    from app.service.task_import import convert, parse_upload

    root = parse_upload(
        "task.xml",
        b"""<task name="uuid"><parameters>
      <parameter type="function"><name>id</name><value>generateUUID()</value></parameter>
      <parameter type="fixed"><name>literal</name><value>generateUUID()</value></parameter>
      </parameters><taskStep order="1"><wait milliseconds="1"/></taskStep></task>""",
    )
    config = convert(root, "http://localhost", lambda *args: None)
    assert [p["source_type"] for p in config["global_parameters"]] == ["function", "fixed"]
    assert all(p["value"] == "generateUUID()" for p in config["global_parameters"])
