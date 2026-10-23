"""Strict XML validation and conversion into the existing task write model."""

import json
from pathlib import Path

from lxml import etree

from app.service.errors import BusinessError, invalid
from app.service.openapi_import import MAX_BYTES, pointer
from app.service.task_config import TYPES, global_binding_name

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs/task.xsd"
MAX_TASK_FILES = 20


def file_issue(index, filename, exc):
    """Rewrap a per-file BusinessError with the file identity in its location."""
    issues = exc.payload["issues"]
    location = issues[0]["location"] if issues else None
    if location is None:
        return exc
    prefix = f"body.file.{index} ({filename})"
    if location == "body.file" or location.startswith("body.file "):
        location = prefix + location[len("body.file"):]
    elif location.startswith("body."):
        location = prefix + "." + location[len("body."):]
    else:
        location = f"{prefix}: {location}"
    return BusinessError(exc.status, exc.payload["code"], exc.payload["detail"], location)


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
            error = schema.error_log.last_error
            invalid(
                "XML 不符合 task.xsd：" + error.message,
                f"{error.path or '/task'} (line {error.line})",
            )
        return root
    except (etree.XMLSyntaxError, ValueError) as exc:
        invalid("XML 格式无效", f"body.file (line {getattr(exc, 'lineno', 1)})")


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
                "source_type": node.get("type"),
                "value_type": node.get("valueType", "string"),
                "value": typed_value(node),
                "sensitive": node.get("sensitive", "false") in {"true", "1"},
            }
        )

    def append(container, scope="main", parent=None):
        for node in sorted(container.findall("taskStep"), key=lambda n: int(n.get("order"))):
            index = len(config["steps"])
            child = next(c for c in node if isinstance(c.tag, str))
            kind = child.tag
            options = {}
            if any(
                node.get(key) is not None
                for key in ("ifParameter", "ifOperator", "ifValue", "onFalse")
            ):
                condition = {
                    "parameter": node.get("ifParameter"),
                    "operator": node.get("ifOperator", "notEmpty"),
                    "on_false": node.get("onFalse", "skip"),
                }
                if node.get("ifValue") is not None:
                    try:
                        condition["value"] = json.loads(node.get("ifValue"))
                    except ValueError:
                        invalid("ifValue 必须为 JSON 字面量", root.getroottree().getpath(node))
                options["condition"] = condition
            step = {"step_type": kind, "execution_scope": scope, "position": int(node.get("order"))}
            if parent is not None:
                step["parent_step_index"] = parent
            step["options"] = options
            config["steps"].append(step)
            if kind == "sql":
                options["sql"] = {
                    "driver": child.get("driver"),
                    "datasource": child.findtext("datasource"),
                    "statement": child.findtext("statement"),
                    "timeout_seconds": int(child.get("timeoutSeconds", "30")),
                }
                if child.find("parameters") is not None:
                    options["sql"]["parameters"] = [
                        item.text.strip() for item in child.findall("parameters/parameter")
                    ]
                for key in ("username", "password"):
                    if child.find(key) is not None:
                        options["sql"][key] = child.findtext(key) or ""
            elif kind == "compute":
                options["timezone"] = child.get("timezone", "Asia/Shanghai")
                options["expressions"] = []
                for computed in child.findall("parameter"):
                    name = computed.findtext("name").strip()
                    options["expressions"].append(
                        {"name": name, "expression": computed.findtext("value").strip()}
                    )
                    config["global_parameters"].append(
                        {
                            "name": name,
                            "source_type": "expression",
                            "value_type": computed.get("valueType", "string"),
                            "sensitive": computed.get("sensitive", "false") in {"true", "1"},
                            "expression_source": {"step_index": index},
                        }
                    )
            elif kind == "wait":
                if child.get("milliseconds") is not None:
                    step["wait_milliseconds"] = int(child.get("milliseconds"))
                if child.get("parameter") is not None:
                    policy = {
                        "parameter": child.get("parameter"),
                        "mode": child.get("mode", "duration"),
                        "unit": child.get("unit", "seconds"),
                        "offset_milliseconds": int(child.get("offsetMilliseconds", "0")),
                    }
                    if child.get("offsetParameter") is not None:
                        policy.update(
                            offset_parameter=child.get("offsetParameter"),
                            offset_unit=child.get("offsetUnit", "seconds"),
                        )
                    options["wait"] = policy
                elif any(
                    child.get(key) is not None
                    for key in (
                        "mode",
                        "unit",
                        "offsetMilliseconds",
                        "offsetParameter",
                        "offsetUnit",
                    )
                ):
                    invalid("动态等待属性必须提供 parameter", root.getroottree().getpath(child))
            elif kind == "parallel":
                step["max_concurrency"] = int(child.get("maxConcurrency", "8"))
                append(child, scope, index)
            else:
                try:
                    interface, _document = lookup(child.get("method"), child.get("path"))
                    step.update(
                        file_id=interface["file_id"],
                        interface_id=interface["id"],
                        version=interface["version"],
                        bindings=[],
                        headers=[],
                        assertion_code=child.findtext("assertionCode"),
                    )
                    poll = child.find("poll")
                    if poll is not None:
                        options["poll"] = {
                            key: int(poll.get(xml_key, default))
                            for key, xml_key, default in (
                                ("max_attempts", "maxAttempts", "10"),
                                ("interval_milliseconds", "intervalMilliseconds", "1000"),
                                ("timeout_seconds", "timeoutSeconds", "60"),
                            )
                        }
                    auth = child.find("auth")
                    if auth is not None:
                        options["auth"] = dict(auth.attrib)
                    for xml_name, field in (("rawPath", "raw_path"), ("rawQuery", "raw_query")):
                        element = child.find(xml_name)
                        if element is not None:
                            options[field] = element.text or ""
                    assignments = child.findall("updateParam")
                    if assignments:
                        options["assignments"] = [
                            {
                                "name": e.findtext("name").strip(),
                                "extract_expression": e.findtext("value").strip(),
                            }
                            for e in assignments
                        ]
                    for header in child.findall("header"):
                        step["headers"].append(
                            {
                                "name": header.findtext("name").strip(),
                                "binding": {"kind": "literal", "value": typed_value(header)},
                            }
                        )
                    for header in step["headers"]:
                        reference = global_binding_name(header["binding"])
                        if reference is not None:
                            header["binding"] = {"kind": "global", "parameter_name": reference}
                    raw = child.find("rawBody")
                    if raw is not None:
                        step["raw_body"] = {"text": raw.text or ""}
                    for container_name, location in (
                        ("pathParams", "path"),
                        ("queryParams", "query"),
                        ("body", "body"),
                    ):
                        for kv in child.findall(container_name + "/kv"):
                            reference = kv.findtext("parameter")
                            name = (kv.findtext("key") or reference or "").strip()
                            if not name:
                                invalid("字面值 kv 必须提供 key", root.getroottree().getpath(kv))
                            if location == "body" and not name.startswith("/"):
                                name = "/" + pointer(name)
                            binding = (
                                {"kind": "global", "parameter_name": reference.strip()}
                                if reference is not None
                                else {"kind": "literal", "value": typed_value(kv)}
                            )
                            step["bindings"].append(
                                {"location": location, "name": name, "binding": binding}
                            )
                    for extracted in child.findall("extractParam"):
                        config["global_parameters"].append(
                            {
                                "name": extracted.findtext("name").strip(),
                                "source_type": "rest",
                                "value_type": extracted.get("valueType", "string"),
                                "sensitive": extracted.get("sensitive", "false") in {"true", "1"},
                                "rest_source": {
                                    "step_index": index,
                                    "extract_expression": extracted.findtext("value").strip(),
                                },
                            }
                        )
                except BusinessError as exc:
                    raise BusinessError(
                        exc.status,
                        exc.payload["code"],
                        exc.payload["detail"],
                        root.getroottree().getpath(child) + f" (line {child.sourceline})",
                    ) from None

    append(root)
    cleanup = root.find("finally")
    if cleanup is not None:
        append(cleanup, "finally")
    if len(config["steps"]) > 1000:
        raise BusinessError(400, "INVALID_STEP_STRUCTURE", "任务节点不得超过 1000", "/task")
    return config
