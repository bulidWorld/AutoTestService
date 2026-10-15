"""Validate Python assertions without executing them; run in a bounded child process."""

import ast
import json
import subprocess
import sys

from app.service.errors import BusinessError, invalid

MAX_CODE_LENGTH = 16000
ASSERTION_TIMEOUT = 3
BUILTINS = [
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "filter",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "map",
    "max",
    "min",
    "range",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AssertionError",
]


def validate_assertion(code, location):
    if code is None or (isinstance(code, str) and not code.strip()):
        return None
    if not isinstance(code, str) or len(code) > MAX_CODE_LENGTH:
        invalid("断言必须为不超过 16000 字符的 Python 代码", location)
    try:
        tree = ast.parse(code)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if len(functions) != len(tree.body):
            raise ValueError("顶层仅允许函数定义")
        entries = [node for node in functions if node.name == "assert_response"]
        if len(entries) != 1 or len({node.name for node in functions}) != len(functions):
            raise ValueError("须定义唯一入口 assert_response(responseBody, globalParam)")
        args = entries[0].args
        if (
            [arg.arg for arg in args.args] != ["responseBody", "globalParam"]
            or args.posonlyargs
            or args.kwonlyargs
            or args.vararg
            or args.kwarg
        ):
            raise ValueError("入口参数须为 responseBody, globalParam")
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.Global,
                    ast.Nonlocal,
                    ast.ClassDef,
                    ast.AsyncFunctionDef,
                    ast.Await,
                    ast.Yield,
                    ast.YieldFrom,
                ),
            ):
                raise TypeError("断言仅支持同步计算，不支持导入模块或类定义")
            if isinstance(node, (ast.FunctionDef, ast.Lambda)) and (
                node.args.defaults or any(v is not None for v in node.args.kw_defaults)
            ):
                raise ValueError("断言函数不支持默认参数")
            if isinstance(node, ast.FunctionDef) and (
                node.decorator_list or node.returns or getattr(node, "type_params", [])
            ):
                raise ValueError("断言函数不支持装饰器或类型注解")
            if isinstance(node, ast.arg) and node.annotation:
                raise ValueError("断言函数不支持类型注解")
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(node, ast.FunctionDef)
                else node.arg
                if isinstance(node, ast.arg)
                else ""
            )
            if name.startswith("_"):
                raise ValueError("断言不支持访问私有名称或属性")
        compile(tree, "<assertion>", "exec")
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        reason = (
            f"断言 Python 语法错误（第 {exc.lineno} 行）"
            if isinstance(exc, SyntaxError)
            else str(exc)
        )
        invalid(reason, location)
    return code


# No application imports or environment are passed to the assertion process.
WORKER = """
import builtins, json, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
try:
    payload = json.load(sys.stdin)
    namespace = {"__builtins__": {name: getattr(builtins, name) for name in payload["builtins"]}}
    exec(compile(payload["code"], "<assertion>", "exec"), namespace)
    result = namespace["assert_response"](payload["body"], payload["values"])
    outcome = ("passed" if result else "failed") if type(result) is bool else "type_error"
except BaseException:
    outcome = "execution_error"
sys.stdout.write(outcome)
"""


def evaluate_assertion(code, body, values):
    """Return only an error code; assertion exceptions never expose response secrets."""
    try:
        validate_assertion(code, "assertion_code")
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", WORKER],
            input=json.dumps({"code": code, "body": body, "values": values, "builtins": BUILTINS}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=ASSERTION_TIMEOUT,
            env={},
            check=False,
        )
        if result.returncode:
            return "ASSERTION_EXECUTION_ERROR"
        return {
            "passed": None,
            "failed": "ASSERTION_FAILED",
            "type_error": "ASSERTION_RETURN_TYPE_ERROR",
        }.get(result.stdout, "ASSERTION_EXECUTION_ERROR")
    except subprocess.TimeoutExpired:
        return "ASSERTION_EXECUTION_ERROR"
    except (OSError, ValueError, TypeError, BusinessError):
        return "ASSERTION_EXECUTION_ERROR"
