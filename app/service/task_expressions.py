"""A bounded expression interpreter: no Python eval, attributes, imports or I/O."""

import ast
import calendar
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.service.errors import BusinessError, invalid

DEFAULT_TIMEZONE = "Asia/Shanghai"
FUNCTIONS = {
    "generateUUID",
    "param",
    "now",
    "today",
    "date",
    "datetime",
    "add_days",
    "add_years",
    "add_seconds",
    "isoformat",
    "http_date",
    "timestamp",
    "timestamp_ms",
    "format_date",
}
ARITY = {
    "generateUUID": (0, 0),
    "param": (1, 1),
    "now": (0, 0),
    "today": (0, 0),
    "date": (1, 1),
    "datetime": (1, 1),
    "add_days": (2, 2),
    "add_years": (2, 2),
    "add_seconds": (2, 2),
    "isoformat": (1, 1),
    "http_date": (1, 1),
    "timestamp": (1, 1),
    "timestamp_ms": (1, 1),
    "format_date": (1, 2),
}
ALLOWED_NODES = (
    ast.Expression,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.UAdd,
    ast.USub,
)


def validate_function_parameter(parameter):
    """Task initializers accept only explicit, zero-argument built-ins."""
    if parameter.get("value_type") != "string" or parameter.get("value") != "generateUUID()":
        invalid("function 参数仅支持 string 类型的 generateUUID()", "global_parameters.value")


def expression_tree(code, location):
    if not isinstance(code, str) or not code.strip() or len(code) > 2000:
        invalid("表达式须为 1～2000 字符", location)
    try:
        tree = ast.parse(code, mode="eval")
        nodes = list(ast.walk(tree))
        if len(nodes) > 128:
            raise ValueError

        def depth(node):
            return 1 + max((depth(child) for child in ast.iter_child_nodes(node)), default=0)

        if depth(tree) > 16:
            raise ValueError
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        for node in nodes:
            if not isinstance(node, ALLOWED_NODES):
                raise ValueError
            if isinstance(node, ast.Name):
                parent = parents.get(node)
                if (
                    node.id not in FUNCTIONS
                    or not isinstance(parent, ast.Call)
                    or parent.func is not node
                ):
                    raise ValueError
            if isinstance(node, ast.Constant):
                if type(node.value) not in (str, int, float, bool, type(None)):
                    raise ValueError
                if isinstance(node.value, str) and len(node.value) > 1024:
                    raise ValueError
                if type(node.value) in (int, float) and (
                    not math.isfinite(node.value) or abs(node.value) > 10**15
                ):
                    raise ValueError
            if isinstance(node, ast.Call):
                if (
                    not isinstance(node.func, ast.Name)
                    or node.func.id not in FUNCTIONS
                    or node.keywords
                ):
                    raise ValueError
                minimum, maximum = ARITY[node.func.id]
                if not minimum <= len(node.args) <= maximum:
                    raise ValueError
                if node.func.id == "param" and (
                    not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                    or not node.args[0].value
                ):
                    raise ValueError
        return tree
    except (SyntaxError, ValueError, TypeError, RecursionError, OverflowError):
        invalid(
            "表达式只允许白名单函数、字面量及有限数值的 + - * /；禁止属性、下标、导入与任意代码",
            location,
        )


def expression_parameters(code, location):
    tree = expression_tree(code, location)
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and node.func.id == "param"
    }


def validate_expressions(step, declarations):
    options = step.get("options") or {}
    expressions = options.get("expressions")
    if step["step_type"] != "compute":
        if expressions is not None or "timezone" in options:
            invalid("expressions/timezone 仅适用于 compute", "steps.options")
        return
    try:
        ZoneInfo(options.get("timezone", DEFAULT_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        invalid("计算节点须使用有效 IANA 时区", "steps.options.timezone")
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= 100:
        invalid("compute 须包含 1～100 个表达式参数", "steps.options.expressions")
    expected = {
        p["name"]
        for p in declarations.values()
        if p["source_type"] == "expression"
        and p.get("expression_source") == {"step_id": step["id"]}
    }
    names = set()
    for index, item in enumerate(expressions):
        location = f"steps.options.expressions.{index}"
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "expression"}
            or not isinstance(item["name"], str)
            or item["name"] not in declarations
            or item["name"] in names
        ):
            invalid("表达式参数须唯一引用本节点声明的参数", location)
        target = declarations[item["name"]]
        if target["source_type"] != "expression" or target.get("expression_source") != {
            "step_id": step["id"]
        }:
            invalid("表达式参数来源必须为当前 compute 节点", location)
        names.add(item["name"])
        references = expression_parameters(item["expression"], location)
        if references - declarations.keys():
            invalid("表达式引用未声明参数", location)
        if references & expected:
            invalid("同一 compute 节点的输出不可互相引用", location)
    if names != expected:
        invalid("compute 表达式必须覆盖本节点的全部参数声明", "steps.options.expressions")


class ExpressionContext:
    def __init__(self, values, timezone):
        # Capture at node execution, never at import, queueing, or preview save time.
        self.instant = datetime.now(UTC).astimezone(ZoneInfo(timezone))
        self.values = values

    def as_date(self, value):
        if isinstance(value, datetime):
            return value.astimezone(self.instant.tzinfo).date()
        if type(value) is date:
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)

    def as_datetime(self, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        return value.astimezone(self.instant.tzinfo)

    def number(self, value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10**15:
            raise ValueError
        return value

    def shifted_year(self, value, years):
        if type(years) is not int or abs(years) > 1000:
            raise ValueError
        if not isinstance(value, (date, datetime)):
            value = self.as_date(value)
        year = value.year + years
        return value.replace(
            year=year, day=min(value.day, calendar.monthrange(year, value.month)[1])
        )

    def call(self, function, arguments):
        if function == "generateUUID":
            return str(uuid4())
        if function == "param":
            if arguments[0] not in self.values:
                raise BusinessError(422, "PARAMETER_NOT_AVAILABLE", "表达式依赖的参数尚未赋值")
            return self.values[arguments[0]]
        if function == "now":
            return self.instant
        if function == "today":
            return self.instant.date()
        if function == "date":
            return self.as_date(arguments[0])
        if function == "datetime":
            return self.as_datetime(arguments[0])
        if function == "add_years":
            return self.shifted_year(*arguments)
        if function == "add_days":
            value, days = arguments
            if type(days) is not int or abs(days) > 366000:
                raise ValueError
            if not isinstance(value, (date, datetime)):
                value = self.as_date(value)
            return value + timedelta(days=days)
        if function == "add_seconds":
            value = self.as_datetime(arguments[0])
            seconds = self.number(arguments[1])
            if abs(seconds) > 366000 * 86400:
                raise ValueError
            return (value.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(
                self.instant.tzinfo
            )
        if function in {"timestamp", "timestamp_ms"}:
            return math.floor(
                self.as_datetime(arguments[0]).timestamp()
                * (1000 if function == "timestamp_ms" else 1)
            )
        if function == "http_date":
            return format_datetime(self.as_datetime(arguments[0]).astimezone(UTC), usegmt=True)
        value = arguments[0]
        if not isinstance(value, (date, datetime)):
            value = self.as_date(value)
        if function == "isoformat":
            return value.isoformat()
        if function == "format_date":
            pattern = arguments[1] if len(arguments) == 2 else "%Y-%m-%d"
            if (
                not isinstance(pattern, str)
                or len(pattern) > 128
                or re.search(r"%(?![YmdHMSfz%])", pattern)
            ):
                raise ValueError
            return value.strftime(pattern)
        raise ValueError

    def evaluate(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Call):
            return self.call(node.func.id, [self.evaluate(arg) for arg in node.args])
        if isinstance(node, ast.UnaryOp):
            value = self.number(self.evaluate(node.operand))
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = (
                self.number(self.evaluate(node.left)),
                self.number(self.evaluate(node.right)),
            )
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            else:
                value = left / right
            return self.number(value)
        raise ValueError


def compute_values(config, step, values):
    from app.service.task_config import TYPES

    options = step["options"]
    context = ExpressionContext(values, options.get("timezone", DEFAULT_TIMEZONE))
    declarations = {p["name"]: p for p in config["global_parameters"]}
    writes = {}
    try:
        for item in options["expressions"]:
            result = context.evaluate(expression_tree(item["expression"], "expression").body)
            target = declarations[item["name"]]
            if not TYPES[target["value_type"]](result):
                raise ValueError
            json.dumps(result, allow_nan=False)
            writes[item["name"]] = result
    except BusinessError:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        raise BusinessError(
            422, "EXPRESSION_EVALUATION_ERROR", "表达式计算失败或结果不符合声明类型"
        ) from None
    return writes
