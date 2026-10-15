"""Strict XML validation and conversion into the existing task write model."""

import json
from pathlib import Path

from lxml import etree

from app.service.errors import BusinessError, invalid
from app.service.openapi_import import MAX_BYTES, pointer
from app.service.task_config import TYPES, resolve_schema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs/task.xsd"


def parse_upload(filename, content):
    if len(content) > MAX_BYTES:
        raise BusinessError(413, "FILE_TOO_LARGE", "文件不得超过 5 MiB", "body.file")
    if not filename or not filename.lower().endswith(".xml"):
        invalid("必须上传 .xml 文件", "body.file")
    try:
        root = etree.fromstring(
            content, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        )
        if root.getroottree().docinfo.doctype:
            invalid("XML 禁止 DTD 和实体", "body.file")
        schema = etree.XMLSchema(etree.parse(str(SCHEMA_PATH)))
        if not schema.validate(root):
            invalid("XML 不符合 task.xsd 格式", "body.file")
        return root
    except (etree.XMLSyntaxError, ValueError):
        invalid("XML 格式无效", "body.file")


def typed_value(node):
    kind = node.get("valueType", "string")
    value = node.findtext("value") or ""
    if kind != "string":
        try:
            value = json.loads(value)
        except ValueError:
            invalid("value 必须是对应类型的 JSON 字面量", "body.file")
    if not TYPES[kind](value):
        invalid("value 与 valueType 不匹配", "body.file")
    return value


def convert(root, base_url, lookup):
    config = {
        "name": root.get("name").strip(),
        "base_url": base_url,
        "global_parameters": [],
        "steps": [],
    }
    for node in root.findall("parameters/parameter"):
        config["global_parameters"].append(
            {
                "name": node.findtext("name").strip(),
                "source_type": "fixed",
                "value_type": node.get("valueType", "string"),
                "value": typed_value(node),
            }
        )
    for index, node in enumerate(
        sorted(root.findall("taskStep"), key=lambda n: int(n.get("order")))
    ):
        rest = node.find("rest")
        interface, document = lookup(rest.get("method"), rest.get("path"))
        step = {
            "file_id": interface["file_id"],
            "interface_id": interface["id"],
            "version": interface["version"],
            "bindings": [],
            "assertion_code": rest.findtext("assertionCode"),
        }
        for header in rest.findall("header"):
            step["bindings"].append(
                {
                    "location": "header",
                    "name": header.findtext("name").strip(),
                    "binding": {"kind": "literal", "value": typed_value(header)},
                }
            )
        for kv in rest.findall("body/kv"):
            reference = kv.findtext("parameter")
            name = (kv.findtext("key") or reference or "").strip()
            if not name:
                invalid("字面值 kv 必须提供 key", "body.file")
            candidates = [
                (p["location"], p["name"])
                for p in interface["parameters"]
                if p["name"] == name and p["location"] in {"path", "query"}
            ]
            bodydef = interface.get("request_body")
            if bodydef:
                properties = resolve_schema(bodydef["schema"], document).get("properties", {})
                if name.startswith("/"):
                    candidates.append(("body", name))
                elif name in properties:
                    candidates.append(("body", "/" + pointer(name)))
            if kv.get("location"):
                candidates = [c for c in candidates if c[0] == kv.get("location")]
            if len(candidates) != 1:
                invalid("kv 字段不存在或存在歧义，请使用 location 和 JSON Pointer", "body.file")
            location, field = candidates[0]
            binding = (
                {"kind": "global", "parameter_name": reference.strip()}
                if reference is not None
                else {"kind": "literal", "value": typed_value(kv)}
            )
            step["bindings"].append({"location": location, "name": field, "binding": binding})
        config["steps"].append(step)
        for extracted in rest.findall("extractParam"):
            config["global_parameters"].append(
                {
                    "name": extracted.findtext("name").strip(),
                    "source_type": "rest",
                    "value_type": extracted.get("valueType", "string"),
                    "rest_source": {
                        "step_index": index,
                        "extract_expression": extracted.findtext("value").strip(),
                    },
                }
            )
    return config
