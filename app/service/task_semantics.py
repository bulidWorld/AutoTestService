"""Explicit execution policies shared by XML, API validation and the worker."""

import math
from datetime import UTC, datetime

from app.service.errors import BusinessError, invalid

MAX_WAIT_MS = 30 * 24 * 60 * 60 * 1000
OPTION_KEYS = {
    "sql",
    "auth",
    "condition",
    "assignments",
    "wait",
    "raw_path",
    "raw_query",
    "expressions",
    "timezone",
}


def validate_options(step, declarations):
    options = step.get("options") or {}
    if not isinstance(options, dict) or options.keys() - OPTION_KEYS:
        invalid("options 含未知执行策略", "steps.options")
    from app.service.task_sql import validate_sql

    validate_sql(step)
    kind = step["step_type"]
    if kind != "rest" and options.keys() & {"auth", "assignments", "raw_path", "raw_query"}:
        invalid("鉴权、参数更新和原始路径查询仅适用于 REST", "steps.options")
    if kind != "wait" and "wait" in options:
        invalid("动态等待仅适用于 wait", "steps.options.wait")
    condition = options.get("condition")
    if condition is not None:
        if (
            not isinstance(condition, dict)
            or condition.keys() - {"parameter", "operator", "value", "on_false"}
            or condition.get("parameter") not in declarations
            or condition.get("operator", "notEmpty")
            not in {"notEmpty", "present", "equals", "truthy"}
            or condition.get("on_false", "skip") not in {"skip", "blocked"}
            or (condition.get("operator") == "equals" and "value" not in condition)
        ):
            invalid(
                "条件须引用已声明参数并指定合法运算及 skip/blocked 策略", "steps.options.condition"
            )
    auth = options.get("auth")
    if auth is not None:
        if not isinstance(auth, dict) or auth.get("mode") not in {"none", "bearer", "header"}:
            invalid("auth.mode 须为 none/bearer/header", "steps.options.auth")
        allowed = {"none": {"mode"}, "bearer": {"mode", "parameter"}, "header": {"mode"}}
        if set(auth) != allowed[auth["mode"]]:
            invalid("鉴权字段不符合所选模式", "steps.options.auth")
        if auth["mode"] == "bearer":
            declaration = declarations.get(auth["parameter"])
            if not declaration or declaration["value_type"] != "string":
                invalid("Bearer 须引用已声明的 string 参数", "steps.options.auth.parameter")
            declaration["sensitive"] = True
    for key, location in (("raw_path", "path"), ("raw_query", "query")):
        if key not in options:
            continue
        raw = options[key]
        if (
            not isinstance(raw, str)
            or not raw.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in raw)
            or "#" in raw
            or (key == "raw_path" and (not raw.startswith("/") or "?" in raw))
            or (key == "raw_query" and raw.startswith("?"))
            or any(f["location"] == location for f in step.get("bindings", []))
        ):
            invalid(
                "原始路径/查询须为无空白控制字符的 ASCII，且不能与同位置绑定混用",
                "steps.options." + key,
            )
    wait = options.get("wait")
    if wait is not None:
        if (
            not isinstance(wait, dict)
            or wait.keys()
            - {
                "parameter",
                "mode",
                "unit",
                "offset_milliseconds",
                "offset_parameter",
                "offset_unit",
            }
            or wait.get("parameter") not in declarations
            or wait.get("mode", "duration") not in {"duration", "until"}
            or wait.get("unit", "seconds") not in {"seconds", "milliseconds", "iso8601"}
            or type(wait.get("offset_milliseconds", 0)) is not int
            or abs(wait.get("offset_milliseconds", 0)) > MAX_WAIT_MS
            or (wait.get("mode", "duration") == "duration" and wait.get("unit") == "iso8601")
            or step.get("wait_milliseconds") is not None
            or ("offset_parameter" in wait and wait["offset_parameter"] not in declarations)
            or wait.get("offset_unit", "seconds") not in {"seconds", "milliseconds"}
        ):
            invalid(
                "动态等待须指定参数、duration/until、单位及可选毫秒偏移，不能同时配置固定等待",
                "steps.options.wait",
            )
    assignments = options.get("assignments", [])
    if not isinstance(assignments, list):
        invalid("参数更新须为数组", "steps.options.assignments")
    names = set()
    from app.service.task_config import expression_tokens

    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or set(assignment) != {"name", "extract_expression"}
            or assignment["name"] not in declarations
            or assignment["name"] in names
        ):
            invalid("updateParam 必须唯一引用已声明参数", "steps.options.assignments")
        expression_tokens(assignment["extract_expression"])
        names.add(assignment["name"])


def condition_action(step, values):
    condition = (step.get("options") or {}).get("condition")
    if not condition:
        return None
    name = condition["parameter"]
    value = values.get(name)
    operator = condition.get("operator", "notEmpty")
    matches = {
        "present": name in values,
        "notEmpty": value is not None and value != "" and value != [] and value != {},
        "truthy": bool(value),
        "equals": name in values and value == condition.get("value"),
    }[operator]
    return None if matches else condition.get("on_false", "skip")


def wait_milliseconds(step, values):
    policy = (step.get("options") or {}).get("wait")
    if not policy:
        return step["wait_milliseconds"]
    value = values.get(policy["parameter"])
    unit = policy.get("unit", "seconds")
    mode = policy.get("mode", "duration")
    try:
        if unit == "iso8601":
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError
            milliseconds = instant.timestamp() * 1000
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError
            milliseconds = value * (1000 if unit == "seconds" else 1)
        milliseconds += policy.get("offset_milliseconds", 0)
        if "offset_parameter" in policy:
            offset = values.get(policy["offset_parameter"])
            if type(offset) not in (int, float) or not math.isfinite(offset):
                raise ValueError
            milliseconds += offset * (
                1000 if policy.get("offset_unit", "seconds") == "seconds" else 1
            )
        if mode == "until":
            milliseconds -= datetime.now(UTC).timestamp() * 1000
        if not math.isfinite(milliseconds) or milliseconds > MAX_WAIT_MS:
            raise ValueError
        return max(0, math.ceil(milliseconds))
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise BusinessError(
            422, "WAIT_VALUE_INVALID", "动态等待值为空、类型错误或超过 30 天"
        ) from None


def auth_headers(step, values, declarations=None):
    auth = (step.get("options") or {}).get("auth")
    if not auth or auth["mode"] in {"none", "header"}:
        return []
    if declarations is not None:
        if auth["parameter"] not in declarations:
            invalid("Bearer 参数尚未可用", "steps.options.auth")
        value = "Bearer validation-placeholder"
    else:
        token = (values or {}).get(auth["parameter"])
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
        ):
            raise BusinessError(422, "AUTH_TOKEN_UNAVAILABLE", "Bearer 参数为空或不是合法令牌")
        value = "Bearer " + token
    return [{"name": "Authorization", "value": value}]
