"""Build a self-contained group snapshot without adding outside requests."""

import ast
import copy

from app.service.errors import invalid
from app.service.task_config import global_binding_name, validate_config


def assertion_parameters(code, declarations):
    """Resolve literal dictionary reads; dynamic access needs all declarations."""
    if not code or not code.strip():
        return set()
    # Syntax and supported Python constructs are checked by validate_config.
    from app.service.assertions import validate_assertion

    validate_assertion(code, "assertion_code")
    tree = ast.parse(code)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    result = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != "globalParam":
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Subscript) and parent.value is node:
            key = parent.slice
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(parent)
            if not isinstance(call, ast.Call) or not call.args:
                return set(declarations)
            key = call.args[0]
        else:
            return set(declarations)
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return set(declarations)
        result.add(key.value)
    return result


def group_configuration(config, group):
    result = copy.deepcopy(config)
    by_id = {step["id"]: step for step in result["steps"]}
    if not group["step_ids"]:
        invalid("空组不能运行", "body.step_ids")
    if len(set(group["step_ids"])) != len(group["step_ids"]) or any(
        sid not in by_id for sid in group["step_ids"]
    ):
        invalid("组成员重复或不属于当前任务", "body.step_ids")
    result["steps"] = [by_id[sid] for sid in group["step_ids"]]
    declarations = {p["name"]: p for p in result["global_parameters"]}
    needed = set()
    for position, step in enumerate(result["steps"], 1):
        step["position"] = position
        needed.update(
            name
            for field in step["bindings"]
            if (name := global_binding_name(field["binding"])) is not None
        )
        needed.update(assertion_parameters(step.get("assertion_code"), declarations))
    if needed - declarations.keys():
        invalid("组步骤引用的全局参数不存在", "steps.bindings")
    result["global_parameters"] = [
        p for p in result["global_parameters"] if p["source_type"] == "fixed" or p["name"] in needed
    ]
    positions = {s["id"]: s["position"] for s in result["steps"]}
    for step in result["steps"]:
        for name in assertion_parameters(step.get("assertion_code"), declarations):
            p = declarations[name]
            if p["source_type"] == "rest":
                source_position = positions.get(p["rest_source"]["step_id"])
                if source_position is None or source_position >= step["position"]:
                    invalid("断言参数源步骤必须在组内且先于使用者", "assertion_code")
    sources = {
        p["rest_source"]["step_id"]
        for p in result["global_parameters"]
        if p["source_type"] == "rest"
    }
    for step in result["steps"]:
        step["execution_phase"] = "parameters" if step["id"] in sources else "steps"
    for position, parameter in enumerate(result["global_parameters"], 1):
        parameter["position"] = position
    result["group"] = copy.deepcopy(group)
    validate_config(result, run=True)
    return result
