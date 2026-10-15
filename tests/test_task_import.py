import copy

import pytest

from app.service.errors import BusinessError
from app.service.task_config import validate_config
from app.service.task_import import MAX_BYTES, convert, parse_upload


def parse(xml):
    return parse_upload("task.xml", xml.encode())


@pytest.mark.parametrize(
    "xml",
    [
        '<task name="x"/>',
        '<task name="x"><taskStep order="0"><rest method="GET" path="/"/></taskStep></task>',
        '<task name="x"><taskStep order="1"><rest method="GET" path="/"/><rest method="GET" path="/"/></taskStep></task>',
        '<task name="x"><taskStep order="1"><rest method="GET" path="/"/></taskStep><taskStep order="1"><rest method="GET" path="/"/></taskStep></task>',
        '<!DOCTYPE task [<!ENTITY x SYSTEM "file:///etc/passwd">]><task name="x"><taskStep order="1"><rest method="GET" path="/"/></taskStep></task>',
        "<task",
    ],
)
def test_reject_invalid_xml(xml):
    with pytest.raises(BusinessError) as error:
        parse(xml)
    assert error.value.status == 400


def test_size_and_extension():
    with pytest.raises(BusinessError) as error:
        parse_upload("task.xml", b"x" * (MAX_BYTES + 1))
    assert error.value.status == 413
    with pytest.raises(BusinessError):
        parse_upload("task.yaml", b"<task/>")


def lookup(method, path):
    return {
        "id": 1,
        "file_id": 1,
        "version": 1,
        "method": method,
        "path": path,
        "parameters": (
            [{"name": "id", "location": "path", "required": True, "schema": {"type": "integer"}}]
            if "{id}" in path
            else []
        ),
        "request_body": None,
    }, {}


def test_sorted_steps_extraction_and_typed_binding():
    root = parse("""<task name="test">
      <parameters><parameter type="fixed" valueType="boolean"><name>flag</name><value>true</value></parameter></parameters>
      <taskStep order="20"><rest method="GET" path="/orders/{id}"><body><kv><parameter>id</parameter></kv></body></rest></taskStep>
      <taskStep order="10"><rest method="GET" path="/init"><extractParam valueType="integer"><name>id</name><value>data[0].id</value></extractParam></rest></taskStep>
    </task>""")
    config = convert(root, "http://localhost", lookup)
    assert config["global_parameters"][0]["value"] is True
    assert config["global_parameters"][1]["rest_source"]["step_index"] == 0
    for index, step in enumerate(config["steps"]):
        step["id"] = index + 1
        step["interface"], step["document"] = lookup(
            "GET", "/init" if index == 0 else "/orders/{id}"
        )
    source = config["global_parameters"][1]["rest_source"]
    source.pop("step_index")
    source["step_id"] = 1
    validate_config(config)
    reversed_config = copy.deepcopy(config)
    reversed_config["steps"].reverse()
    with pytest.raises(BusinessError):
        validate_config(reversed_config)


@pytest.mark.parametrize("kind,value", [("integer", "true"), ("number", "NaN"), ("object", "[]")])
def test_wrong_typed_values(kind, value):
    root = parse(
        f'''<task name="x"><parameters><parameter type="fixed" valueType="{kind}"><name>x</name><value>{value}</value></parameter></parameters><taskStep order="1"><rest method="GET" path="/init"/></taskStep></task>'''
    )
    with pytest.raises(BusinessError):
        convert(root, "http://localhost", lookup)
