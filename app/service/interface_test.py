"""Execute an interface asset once using typed literal inputs."""

import copy
import json
from datetime import UTC, datetime

from app.service.openapi_import import SENSITIVE, redact
from app.service.task_config import (
    SECRET_HEADERS,
    build_request,
    scrub,
    secret_strings,
    validate_config,
)
from app.service.task_execution import request_http


def execute_once(interface, document, inputs, project_id):
    fields = [
        {
            "location": p["location"],
            "name": p["name"],
            "binding": {"kind": "literal", "value": p["value"]},
        }
        for p in inputs["parameters"]
    ]
    if "request_body" in inputs:
        fields.append(
            {
                "location": "body",
                "name": "",
                "binding": {"kind": "literal", "value": inputs["request_body"]},
            }
        )
    step = {"id": interface["id"], "interface": interface, "document": document, "bindings": fields}
    config = {"base_url": inputs["base_url"], "global_parameters": [], "steps": [step]}
    validate_config(config, run=True)
    request = build_request(config, step, values={})
    secrets = []
    for parameter in inputs["parameters"]:
        if parameter.get("sensitive") or SENSITIVE.search(parameter["name"]):
            secrets.extend(secret_strings(parameter["value"]))
    if inputs.get("body_sensitive") and "request_body" in inputs:
        secrets.extend(secret_strings(inputs["request_body"]))
    started_at = datetime.now(UTC)
    response = request_http(request, inputs.get("timeout_seconds", 30))
    result = {
        "project_id": project_id,
        "file_id": interface["file_id"],
        "interface_id": interface["id"],
        "version": interface["version"],
        "status": "failed" if response["error_code"] else "succeeded",
        **request,
        **response,
        "started_at": started_at,
        "finished_at": datetime.now(UTC),
    }
    original = copy.deepcopy(result)
    for key in ["request_headers", "response_headers"]:
        for header in result[key] or []:
            header["redacted"] = header["name"].lower() in SECRET_HEADERS or bool(
                SENSITIVE.search(header["name"])
            )
            if header["redacted"]:
                secrets.extend(secret_strings(header["value"]))
                header["value"] = "***"
    for key in ["request_body", "response_body"]:
        if result[key] is not None:
            if inputs.get("body_sensitive"):
                result[key] = (
                    "Kioq"
                    if key == "response_body" and result["response_body_encoding"] == "base64"
                    else "***"
                )
            elif (
                key == "response_body" and result["response_body_encoding"] == "base64" and secrets
            ):
                result[key] = "Kioq"
            else:
                try:
                    parsed = json.loads(result[key])
                    safe = redact(parsed)
                    if safe != parsed:
                        result[key] = json.dumps(safe, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
    for key in ["actual_url", "request_body", "response_body", "error_message"]:
        result[key] = scrub(result[key], secrets)
    for key in ["request_headers", "response_headers"]:
        for header in result[key] or []:
            safe = scrub(header["value"], secrets)
            header["redacted"] = header["redacted"] or safe != header["value"]
            header["value"] = safe
    # Flags on ordinary headers alone do not count as redaction.
    for key in ["request_headers", "response_headers"]:
        for header in original[key] or []:
            header["redacted"] = False
    result["redacted"] = result != original
    return result
