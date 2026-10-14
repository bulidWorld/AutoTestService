"""Task configuration validation, typed bindings and safe response extraction."""

import copy
import json
import math
import re
from urllib.parse import quote, urlencode, urlsplit

from jsonschema import Draft4Validator

from app.service.errors import BusinessError, invalid

TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: (
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    ),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}
EXPRESSION = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[(?:0|[1-9][0-9]*)\])*\Z"
)
SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
FORBIDDEN_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "method",
    "url",
    "base_url",
    "timeout_seconds",
}


def expression_tokens(expression):
    if not isinstance(expression, str) or not EXPRESSION.fullmatch(expression):
        invalid("取值表达式仅支持点分字段和非负数组下标", "rest_source.extract_expression")
    return [
        int(index) if index else field
        for field, index in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]", expression)
    ]


def extract(document, expression, value_type, location):
    current = document
    try:
        for token in expression_tokens(expression):
            if isinstance(token, int):
                if not isinstance(current, list):
                    raise KeyError
            elif not isinstance(current, dict):
                raise KeyError
            current = current[token]
    except (KeyError, IndexError, TypeError):
        raise BusinessError(422, "EXTRACTION_NOT_FOUND", "响应取值字段不存在", location) from None
    if not TYPES[value_type](current):
        raise BusinessError(422, "EXTRACTION_TYPE_ERROR", "响应取值类型不匹配或为 null", location)
    return current


def validate_json(value, schema, document, location):
    # OpenAPI nullable is not a JSON Schema keyword; task bindings deliberately reject null.
    if value is None:
        invalid("绑定值不得为 null", location)
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        invalid("绑定值必须为有限且可序列化的 JSON 值", location)
    error = next(Draft4Validator(document).evolve(schema=schema).iter_errors(value), None)
    if error:
        invalid("绑定值不符合接口 Schema", location)


def resolve_schema(schema, document):
    visited = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if ref in visited or not ref.startswith("#/"):
            invalid("无法解析 Schema 引用", "steps.bindings")
        visited.add(ref)
        schema = document
        for part in ref[2:].split("/"):
            schema = schema[part.replace("~1", "/").replace("~0", "~")]
    return schema


def pointer_schema(schema, pointer, document):
    parts = [] if pointer == "" else pointer.split("/")[1:]
    if pointer and not pointer.startswith("/"):
        invalid("JSON 字段须使用 JSON Pointer", "steps.bindings.name")
    for part in parts:
        if re.search(r"~(?![01])", part):
            invalid("JSON Pointer 转义无效", "steps.bindings.name")
        schema = resolve_schema(schema, document)
        key = part.replace("~1", "/").replace("~0", "~")
        if schema.get("type") == "array" and key.isdecimal():
            if int(key) > 10000:
                invalid("数组下标过大", "steps.bindings.name")
            schema = schema.get("items", {})
        elif schema.get("type") == "object" and key in schema.get("properties", {}):
            schema = schema["properties"][key]
        else:
            invalid("JSON Pointer 未指向接口字段", "steps.bindings.name")
    return resolve_schema(schema, document)


def sample(schema, document, depth=0):
    schema = resolve_schema(schema, document)
    if depth > 20:
        invalid("Schema 必填递归过深", "steps.bindings")
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {
            k: sample(schema["properties"][k], document, depth + 1)
            for k in schema.get("required", [])
        }
    if kind == "array":
        return [
            sample(schema.get("items", {}), document, depth + 1)
            for _ in range(schema.get("minItems", 0))
        ]
    if kind in ("integer", "number"):
        return schema.get("minimum", 0) + (1 if schema.get("exclusiveMinimum") else 0)
    return {"string": "x" * max(schema.get("minLength", 0), 1), "boolean": False}.get(kind, "")


def assign_body(body, pointer, value, schema, document):
    if pointer == "":
        return value
    parts = [x.replace("~1", "/").replace("~0", "~") for x in pointer.split("/")[1:]]
    if body is None:
        body = [] if resolve_schema(schema, document).get("type") == "array" else {}
    current = body
    for i, key in enumerate(parts):
        last = i == len(parts) - 1
        target_schema = pointer_schema(
            schema, "/" + "/".join(pointer.split("/")[1 : i + 2]), document
        )
        child = [] if target_schema.get("type") == "array" else {}
        if isinstance(current, list):
            index = int(key)
            while len(current) <= index:
                current.append(None)
            if last:
                current[index] = value
            else:
                if current[index] is None:
                    current[index] = child
                current = current[index]
        else:
            if last:
                current[key] = value
            else:
                current = current.setdefault(key, child)
    return body


def global_binding_name(binding):
    """An entire {name} literal is shorthand for a typed global binding."""
    if binding["kind"] == "global":
        return binding["parameter_name"]
    value = binding.get("value")
    if isinstance(value, str):
        match = re.fullmatch(r"\{([^{}]+)\}", value)
        if match:
            return match.group(1)
    return None


def build_request(task, step, values=None, declarations=None):
    """Validate and render one request. Declarations enable static checks without REST calls."""
    interface, document = step["interface"], step["document"]
    bodydef = interface.get("request_body")
    if bodydef and bodydef["content_type"] != "application/json":
        invalid("本阶段不支持 multipart 执行", "steps.bindings")
    parameters = {
        (p["location"], p["name"].lower() if p["location"] == "header" else p["name"]): p
        for p in interface["parameters"]
    }
    bound, query, headers, path, body = set(), [], [], interface["path"], None
    body_fields, dynamic_body = [], False
    for field in step["bindings"]:
        location, name, binding = field["location"], field["name"], field["binding"]
        key = (location, name.lower() if location == "header" else name)
        if key in bound:
            invalid("字段绑定重复", "steps.bindings")
        bound.add(key)
        parameter = parameters.get(key)
        if location == "body":
            if not bodydef:
                invalid("接口无 JSON 请求体", "steps.bindings")
            schema = pointer_schema(bodydef["schema"], name, document)
            if any(
                name == p or name.startswith(p + "/") or p.startswith(name + "/")
                for p in body_fields
            ):
                invalid("JSON 父子字段绑定冲突", "steps.bindings")
            body_fields.append(name)
        elif parameter:
            schema = resolve_schema(parameter["schema"], document)
        elif location == "header":
            schema = {"type": "string"}
        else:
            invalid("绑定的接口参数不存在", "steps.bindings")
        if location == "header" and (
            name.lower() in FORBIDDEN_HEADERS
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
        ):
            invalid("请求头名称无效或覆盖执行配置", "steps.bindings")
        parameter_name = global_binding_name(binding)
        if parameter_name is not None:
            if declarations is not None:
                if parameter_name not in declarations:
                    invalid("全局参数不存在或未初始化", "steps.bindings")
                declaration = declarations[parameter_name]
                kind = declaration["value_type"]
                expected = schema.get("type")
                if (
                    expected
                    and kind != expected
                    and not (kind == "integer" and expected == "number")
                ):
                    invalid("全局参数类型与接口字段不匹配", "steps.bindings")
                if declaration["source_type"] == "fixed":
                    value = declaration["value"]
                    validate_json(value, schema, document, "steps.bindings")
                else:
                    value = sample(schema, document)
                    if location == "body":
                        dynamic_body = True
            else:
                if parameter_name not in (values or {}):
                    invalid("全局参数未初始化", "steps.bindings")
                value = values[parameter_name]
                validate_json(value, schema, document, "steps.bindings")
        else:
            value = binding.get("value")
            validate_json(value, schema, document, "steps.bindings")
        if location == "path":
            if isinstance(value, (dict, list)):
                invalid("path 不支持对象或数组", "steps.bindings")
            path = path.replace("{" + name + "}", quote(stringify(value), safe=""))
        elif location == "query":
            if isinstance(value, dict):
                invalid("不支持对象 query 参数", "steps.bindings")
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, (dict, list)):
                    invalid("query 数组仅支持标量元素", "steps.bindings")
                query.append((name, stringify(item)))
        elif location == "header":
            if (
                isinstance(value, (dict, list))
                or "\r" in stringify(value)
                or "\n" in stringify(value)
            ):
                invalid("请求头须为无换行标量", "steps.bindings")
            headers.append({"name": name, "value": stringify(value)})
        elif location == "body":
            body = assign_body(body, name, value, bodydef["schema"], document)
    for key, parameter in parameters.items():
        if parameter["required"] and key not in bound:
            invalid("缺少必填接口参数 " + parameter["name"], "steps.bindings")
    if "{" in path or "}" in path:
        invalid("路径参数未完整绑定", "steps.bindings")
    if bodydef:
        if body is not None and dynamic_body:
            for error in (
                Draft4Validator(document).evolve(schema=bodydef["schema"]).iter_errors(body)
            ):
                if error.validator == "required":
                    invalid("缺少必填 JSON 字段", "steps.bindings")
        if body is None and bodydef.get("required"):
            invalid("缺少必填 JSON 请求体", "steps.bindings")
        if body is not None and not dynamic_body:
            validate_json(body, bodydef["schema"], document, "steps.bindings")
        for header in headers:
            if (
                body is not None
                and header["name"].lower() == "content-type"
                and header["value"].split(";")[0].strip().lower() != "application/json"
            ):
                invalid("JSON 请求体 Content-Type 必须为 application/json", "steps.bindings")
        if body is not None and not any(h["name"].lower() == "content-type" for h in headers):
            headers.append({"name": "Content-Type", "value": "application/json"})
    if "?" in path or "#" in path or not path.startswith("/"):
        invalid("接口路径不得包含 query 或 fragment", "steps.bindings")
    base = urlsplit(task["base_url"])
    actual_path = base.path.rstrip("/") + "/" + path.lstrip("/")
    url = base._replace(
        path=quote(actual_path, safe="/%:@!$&'()*+,;=-._~"), query=urlencode(query)
    ).geturl()

    return {
        "method": interface["method"],
        "actual_url": url,
        "request_headers": headers,
        "request_body": json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if body is not None
        else None,
    }


def stringify(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, bool) else str(value)


def validate_config(task, run=False):
    try:
        parsed = urlsplit(task["base_url"])
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        invalid("运行地址格式或端口无效", "body.base_url")
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or "?" in task["base_url"]
        or "#" in task["base_url"]
        or not valid_port
        or any(c.isspace() for c in task["base_url"])
    ):
        invalid("运行地址必须为无凭据、query 或 fragment 的 HTTP/HTTPS 地址", "body.base_url")
    if run and not task["steps"]:
        invalid("运行至少一个步骤", "body.steps")
    declarations, steps = {}, {s["id"]: s for s in task["steps"]}
    if len(steps) != len(task["steps"]):
        invalid("步骤 ID 重复", "body.steps")
    for i, p in enumerate(task["global_parameters"]):
        p["name"] = p["name"].strip()
        p.setdefault("source_type", "fixed")
        if not p["name"] or p["name"] in declarations:
            invalid("全局参数名称为空或重复", f"body.global_parameters.{i}.name")
        if p["source_type"] == "fixed" and not TYPES[p["value_type"]](p.get("value")):
            invalid("固定参数类型不匹配或缺少值", f"body.global_parameters.{i}.value")
        declarations[p["name"]] = p
    ready = {}
    for kind, item in execution_plan(task):
        if kind == "parameter":
            ready[item["name"]] = item
        else:
            build_request(task, item, declarations=ready)
    return task


def execution_plan(task):
    """Fixed values first, then each request followed by its response assignments."""
    sources = {}
    step_ids = {step["id"] for step in task["steps"]}
    for parameter in task["global_parameters"]:
        if parameter["source_type"] == "fixed":
            yield "parameter", parameter
            continue
        source = parameter.get("rest_source") or {}
        if set(source) != {"step_id", "extract_expression"}:
            invalid("REST 参数必须绑定本任务步骤及取值表达式", "rest_source")
        expression_tokens(source["extract_expression"])
        if source["step_id"] not in step_ids:
            invalid("REST 引用的步骤不存在或不属于本任务", "rest_source.step_id")
        sources.setdefault(source["step_id"], []).append(parameter)
    for step in task["steps"]:
        yield "step", step
        for parameter in sources.get(step["id"], []):
            yield "parameter", parameter


def sensitive_values(task, values=None):
    result = []
    for p in task["global_parameters"]:
        if p.get("sensitive"):
            value = (values or {}).get(p["name"], p.get("value"))
            if value is not None:
                result.extend(secret_strings(value))
    for step in task["steps"]:
        for field in step["bindings"]:
            if field["location"] == "header" and field["name"].lower() in SECRET_HEADERS:
                b = field["binding"]
                parameter_name = global_binding_name(b)
                if parameter_name is None:
                    value = b.get("value")
                else:
                    parameter = next(
                        (p for p in task["global_parameters"] if p["name"] == parameter_name), {}
                    )
                    value = (values or {}).get(parameter_name, parameter.get("value"))
                if value is not None:
                    result.extend(secret_strings(value))
    return result


def secret_strings(value):
    if isinstance(value, dict):
        return [s for v in value.values() for s in secret_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in secret_strings(v)]
    return [stringify(value)]


def safe_task(task):
    result = copy.deepcopy(task)
    for p in result["global_parameters"]:
        p.pop("task_id", None)
        p["redacted"] = bool(p.get("sensitive") and p["source_type"] == "fixed")
        if p["redacted"]:
            p.pop("value", None)
    for step in result["steps"]:
        for field in step["bindings"]:
            b = field["binding"]
            if b["kind"] == "literal":
                b["redacted"] = (
                    field["location"] == "header" and field["name"].lower() in SECRET_HEADERS
                )
                if b["redacted"]:
                    b.pop("value", None)
        step.pop("interface", None)
        step.pop("document", None)
    secrets = sensitive_values(task)
    for key in ["name", "description", "base_url"]:
        if key in result:
            result[key] = scrub(result[key], secrets)
    for parameter in result["global_parameters"]:
        if "value" in parameter:
            parameter["value"] = scrub(parameter["value"], secrets)
    for step in result["steps"]:
        for field in step["bindings"]:
            binding = field["binding"]
            if binding["kind"] == "literal" and "value" in binding:
                safe = scrub(binding["value"], secrets)
                if safe != binding["value"]:
                    binding.pop("value")
                    binding["redacted"] = True
    return result


def scrub(value, secrets):
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                for variant in {
                    secret,
                    quote(secret, safe=""),
                    quote(secret, safe="").replace("%20", "+"),
                }:
                    value = value.replace(variant, "***")
    return value
