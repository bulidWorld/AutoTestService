"""Bind request validation and handlers to the checked-in OpenAPI contract."""

import json
import logging

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from openapi_schema_validator import OAS30Validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from app.config.logging import request_context
from app.repository.projects import execute
from app.service.errors import BusinessError, invalid
from app.service.openapi_import import MAX_BYTES, parse_upload


def error_response(request, error):
    logging.getLogger(__name__).error(
        "业务请求失败",
        extra={**request_context(request), "status": error.status, **error.payload},
    )
    return JSONResponse(status_code=error.status, content=error.payload)


def validate_value(value, schema, specification, location):
    validator = OAS30Validator(specification).evolve(schema=schema)
    error = next(validator.iter_errors(value), None)
    if error:
        suffix = "".join("." + str(p) for p in error.path)
        invalid("字段类型、长度或取值无效", location + suffix)


def bind(operation, specification, handler, database_free=False):
    async def endpoint(request: Request):
        try:
            path, query, body, upload = {}, {}, {}, None
            for parameter in operation.get("parameters", []):
                if "$ref" in parameter:
                    parameter = specification["components"]["parameters"][
                        parameter["$ref"].split("/")[-1]
                    ]
                name, source = parameter["name"], parameter["in"]
                values = request.path_params if source == "path" else request.query_params
                raw = values.get(name)
                schema = parameter["schema"]
                if raw is None:
                    if parameter.get("required"):
                        invalid("缺少必填参数", source + "." + name)
                    if "default" not in schema:
                        continue
                    raw = schema["default"]
                if schema.get("type") == "integer":
                    try:
                        if isinstance(raw, str) and (not raw.isascii() or not raw.isdecimal()):
                            raise ValueError
                        raw = int(raw)
                    except (TypeError, ValueError):
                        invalid("必须为正整数", source + "." + name)
                elif schema.get("type") == "boolean":
                    if raw not in ("true", "false", True, False):
                        invalid("必须为 true 或 false", source + "." + name)
                    raw = raw in ("true", True)
                validate_value(raw, schema, specification, source + "." + name)
                (path if source == "path" else query)[name] = raw
            if "requestBody" in operation:
                content = operation["requestBody"]["content"]
                media = request.headers.get("content-type", "").split(";")[0].lower()
                if media not in content:
                    invalid("请求 Content-Type 不符合接口定义", "body")
                schema = content[media]["schema"]
                if media == "application/json":
                    try:
                        body = await request.json()
                        json.dumps(body, allow_nan=False)
                    except (ValueError, UnicodeError):
                        invalid("请求必须为有效 JSON", "body")
                    if isinstance(body, dict) and isinstance(body.get("name"), str):
                        body["name"] = body["name"].strip()
                    validate_value(body, schema, specification, "body")
                else:
                    # Bound total request bytes before multipart parsing to avoid unbounded uploads.
                    buffer = bytearray()
                    async for chunk in request.stream():
                        buffer.extend(chunk)
                        if len(buffer) > MAX_BYTES + 65536:
                            raise BusinessError(
                                413, "FILE_TOO_LARGE", "文件不得超过 5 MiB", "body.file"
                            )
                    request._body = bytes(buffer)
                    async with request.form(
                        max_files=1, max_fields=2, max_part_size=MAX_BYTES
                    ) as form:
                        if len(form.multi_items()) != len(form):
                            invalid("上传字段不得重复", "body")
                        file = form.get("file")
                        if not isinstance(file, UploadFile):
                            invalid("必须上传文件", "body.file")
                        body = {k: v.strip() if isinstance(v, str) else "" for k, v in form.items()}
                        validate_value(body, schema, specification, "body")
                        parser = parse_upload
                        if operation["operationId"] == "importTask":
                            from app.service.task_import import parse_upload as parse_task_upload

                            parser = parse_task_upload
                        upload = await run_in_threadpool(
                            parser, file.filename, await file.read(MAX_BYTES + 1)
                        )
            if database_free:
                result = await run_in_threadpool(handler, None, path, query, body, upload)
            else:
                result = await run_in_threadpool(execute, handler, path, query, body, upload)
            status = next(
                int(code)
                for code in operation["responses"]
                if code.isdigit() and 200 <= int(code) < 300
            )
            return JSONResponse(status_code=status, content=jsonable_encoder(result))
        except BusinessError as exc:
            return error_response(request, exc)
        except HTTPException:
            error = BusinessError(400, "INVALID_REQUEST", "上传表单格式无效或字段过多", "body")
            return error_response(request, error)

    endpoint.__name__ = handler.__name__
    endpoint.__doc__ = handler.__doc__
    endpoint.operation_handler = handler
    return endpoint
