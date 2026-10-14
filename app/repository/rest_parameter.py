"""Operation handlers for parameter preview and supplied-response extraction."""

from app.repository.rest_task import previewRestParameter
from app.service.errors import BusinessError, invalid
from app.service.task_config import expression_tokens, extract
from app.service.task_execution import response_json

SYSTEM_OPERATIONS = set()
DATABASE_FREE_OPERATIONS = {"extractRestParameter"}


def extractRestParameter(repository, path, query, body, upload):
    name = body["parameter_name"].strip()
    if not name:
        invalid("参数名称不能为空", "body.parameter_name")
    expression = body["extract_expression"]
    # Validate before decoding, and locate syntax failures at this API's input field.
    try:
        expression_tokens(expression)
    except BusinessError:
        invalid("取值表达式仅支持点分字段和非负数组下标", "body.extract_expression")
    response = body["response"]
    document = response_json(
        {
            "error_code": None,
            "response_body": response["response_body"],
            "response_body_encoding": response.get("response_body_encoding", "utf-8"),
        },
        "body.response",
    )
    value = extract(document, expression, body["value_type"], "body.extract_expression")
    return {
        "parameter_name": name,
        "value_type": body["value_type"],
        "extract_expression": expression,
        "value": value,
    }


OPERATIONS = {
    "previewRestParameter": previewRestParameter,
    "extractRestParameter": extractRestParameter,
}
