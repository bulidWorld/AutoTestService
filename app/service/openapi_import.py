"""Strict phase-one import, local reference expansion and display redaction."""

import json
import re
from copy import deepcopy
from pathlib import PureWindowsPath

import yaml
from openapi_spec_validator import validate

from app.service.errors import invalid

MAX_BYTES = 5 * 1024 * 1024
REQUEST_MEDIA_TYPES = {"application/json", "multipart/form-data"}
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
SENSITIVE = re.compile(
    r"password|passwd|secret|token|authorization|api[_-]?key|cookie", re.IGNORECASE
)


def pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


class StrictLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=deep)
        if key in result:
            raise ValueError("重复 YAML 字段")
        result[key] = loader.construct_object(value, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def redact(value, sensitive=False):
    if isinstance(value, dict):
        named = bool(SENSITIVE.search(str(value.get("name", ""))))
        result = {}
        for key, item in value.items():
            secret = bool(SENSITIVE.search(str(key)))
            if secret and key not in {"securitySchemes", "security"}:
                # Preserve schema definitions, hide their values recursively.
                result[key] = redact(item, True) if isinstance(item, (dict, list)) else "[REDACTED]"
            elif (sensitive or named) and key in {"default", "example", "examples", "value"}:
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item, sensitive or named)
        return result
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(Bearer[ \t]+|Basic[ \t]+)\S+", r"\1[REDACTED]", value)
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)((?:password|token|api_key|secret)=)[^&\s]+", r"\1[REDACTED]", value)
    return value


def parse_upload(filename, content):
    filename = PureWindowsPath(filename or "").name
    if not filename.lower().endswith((".yaml", ".yml")):
        invalid("仅支持 .yaml 或 .yml 文件", "body.file")
    if len(content) > MAX_BYTES:
        from app.service.errors import BusinessError

        raise BusinessError(413, "FILE_TOO_LARGE", "文件不得超过 5 MiB", "body.file")
    try:
        original = content.decode("utf-8-sig")
        document = yaml.load(original, Loader=StrictLoader)
    except (UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        invalid("文件必须为有效 UTF-8 YAML，且不得包含重复字段", "/", "INVALID_YAML")
    if not isinstance(document, dict):
        invalid("YAML 文档不能为空且必须为对象", "/", "INVALID_YAML")
    if not re.fullmatch(r"3\.0\.\d+", str(document.get("openapi", ""))):
        invalid("仅支持 OpenAPI 3.0.x", "/openapi", "INVALID_OPENAPI")

    visited = 0

    def expand(value, location="", stack=()):
        nonlocal visited
        visited += 1
        if visited > 100000 or location.count("/") > 150:
            invalid("文档展开后过大或嵌套过深", location, "INVALID_OPENAPI")
        if location.rsplit("/", 1)[-1] in {"example", "examples", "default", "enum"}:
            return deepcopy(value)
        if isinstance(value, list):
            return [expand(v, f"{location}/{i}", stack) for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return value
        collection = location.rsplit("/", 1)[-1] in {
            "properties",
            "schemas",
            "responses",
            "content",
            "securitySchemes",
            "headers",
            "requestBodies",
        }
        if "$ref" in value and not collection:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/components/"):
                invalid("仅支持文件内 components 引用", location + "/$ref", "UNSUPPORTED_SCHEMA")
            if ref in stack:
                # Recursive schemas retain the local reference at the cycle boundary.
                return {"$ref": ref}
            if len(stack) > 64:
                invalid("引用嵌套过深", location + "/$ref", "INVALID_OPENAPI")
            target = document
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                invalid("引用无法解析", location + "/$ref", "INVALID_OPENAPI")
            return expand(target, location, stack + (ref,))
        for key in ("oneOf", "anyOf", "allOf", "not"):
            if key in value and not collection:
                invalid("暂不支持组合 Schema", location + "/" + key, "UNSUPPORTED_SCHEMA")
        if value.get("in") == "cookie":
            invalid("暂不支持 cookie 参数", location, "UNSUPPORTED_SCHEMA")
        return {k: expand(v, location + "/" + pointer(k), stack) for k, v in value.items()}

    try:
        expanded = expand(document)
        validate(document)
        json.dumps(document, allow_nan=False)
    except (RecursionError, TypeError, ValueError) as exc:
        from app.service.errors import BusinessError

        if isinstance(exc, BusinessError):
            raise
        invalid("文档结构无效或嵌套过深", "/", "INVALID_OPENAPI")
    except Exception as exc:
        from app.service.errors import BusinessError

        if isinstance(exc, BusinessError):
            raise
        path = "/" + "/".join(pointer(p) for p in getattr(exc, "path", []))
        invalid("OpenAPI 字段或结构无效", path, "INVALID_OPENAPI")

    def check_body(body, location):
        content = body.get("content", {})
        for media_type in content:
            if media_type not in REQUEST_MEDIA_TYPES:
                invalid(
                    "请求体仅支持 application/json 或 multipart/form-data",
                    location + "/content/" + pointer(media_type),
                    "UNSUPPORTED_SCHEMA",
                )
        if len(content) != 1:
            invalid("请求体必须定义一种媒体类型", location + "/content", "UNSUPPORTED_SCHEMA")

    # Inspect actual operations and reusable request bodies, not example payloads.
    for path, item in expanded.get("paths", {}).items():
        for method, operation in item.items():
            if method in METHODS and "requestBody" in operation:
                check_body(
                    operation["requestBody"],
                    "/paths/" + pointer(path) + "/" + method + "/requestBody",
                )
    for name, body in expanded.get("components", {}).get("requestBodies", {}).items():
        check_body(body, "/components/requestBodies/" + pointer(name))
    interfaces = []
    for path, item in expanded.get("paths", {}).items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            params = {(p["in"], p["name"]): p for p in item.get("parameters", [])}
            params.update({(p["in"], p["name"]): p for p in operation.get("parameters", [])})
            parameters = []
            for p in params.values():
                if "schema" not in p:
                    invalid(
                        "参数必须使用 schema 定义",
                        "/paths/" + pointer(path) + "/parameters",
                        "UNSUPPORTED_SCHEMA",
                    )
                schema = deepcopy(p["schema"])
                for key in ("example", "examples"):
                    if key in p:
                        schema[key] = p[key]
                parameters.append(
                    {
                        "name": p["name"],
                        "location": p["in"],
                        "description": p.get("description", ""),
                        "required": p.get("required", False),
                        "schema": schema,
                    }
                )
            record = {
                "method": method.upper(),
                "path": path,
                "operation_id": operation.get("operationId", ""),
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "parameters": parameters,
                "responses": operation["responses"],
                "authentication": {
                    "schemes": expanded.get("components", {}).get("securitySchemes", {}),
                    "requirements": operation.get("security", expanded.get("security", [])),
                },
                "servers": operation.get(
                    "servers", item.get("servers", expanded.get("servers", []))
                ),
            }
            if "requestBody" in operation:
                body = operation["requestBody"]
                content_type, media = next(iter(body["content"].items()))
                schema = deepcopy(media.get("schema", {}))
                for key in ("example", "examples"):
                    if key in media:
                        schema[key] = media[key]
                record["request_body"] = {
                    "description": body.get("description", ""),
                    "required": body.get("required", False),
                    "content_type": content_type,
                    "schema": schema,
                }
                if "encoding" in media:
                    record["request_body"]["encoding"] = deepcopy(media["encoding"])
            interfaces.append(record)
    if not interfaces:
        invalid("文件必须包含至少一个 HTTP 接口", "/paths", "NO_INTERFACES")
    return {
        "filename": filename,
        "original": original,
        "document": document,
        "interfaces": interfaces,
    }
