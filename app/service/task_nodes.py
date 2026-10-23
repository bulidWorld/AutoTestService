"""The single execution-tree model used by saving, import, preview and workers."""

from app.service.errors import BusinessError
from app.service.task_semantics import MAX_WAIT_MS

REST_FIELDS = {
    "interface_id",
    "file_id",
    "version",
    "bindings",
    "headers",
    "raw_body",
    "assertion_code",
}


def structure_error(message, location="body.steps"):
    raise BusinessError(400, "INVALID_STEP_STRUCTURE", message, location)


def siblings(task, scope="main", parent=None):
    return sorted(
        (
            s
            for s in task["steps"]
            if s["execution_scope"] == scope and s.get("parent_step_id") == parent
        ),
        key=lambda s: (s["position"], s["id"]),
    )


def preorder(task, scope=None):
    for current_scope in [scope] if scope else ["main", "finally"]:
        for step in siblings(task, current_scope):
            yield step
            if step["step_type"] == "parallel":
                yield from siblings(task, current_scope, step["id"])


def validate_structure(task):
    steps = task["steps"]
    if len(steps) > 1000:
        structure_error("任务节点不得超过 1000")
    by_id = {s["id"]: s for s in steps}
    if len(by_id) != len(steps):
        structure_error("步骤 ID 重复")
    positions = set()
    for index, step in enumerate(steps):
        location = f"body.steps.{index}"
        kind, scope = step.get("step_type"), step.get("execution_scope")
        if kind not in {"rest", "wait", "parallel", "compute", "sql"} or scope not in {"main", "finally"}:
            structure_error("必须指定合法 step_type 和 execution_scope", location)
        position = step.get("position")
        if type(position) is not int or not 1 <= position <= 2147483647:
            structure_error("position 必须为正 int32", location + ".position")
        parent = step.get("parent_step_id")
        key = (scope, parent, position)
        if key in positions:
            structure_error("同 scope、父节点的 position 重复", location + ".position")
        positions.add(key)
        if parent is not None:
            target = by_id.get(parent)
            if (
                not target
                or parent == step["id"]
                or target.get("step_type") != "parallel"
                or target.get("execution_scope") != "main"
                or target.get("parent_step_id") is not None
                or scope != "main"
                or kind == "parallel"
            ):
                structure_error(
                    "父节点只能为本任务 main 根 parallel，子节点仅允许 main rest/wait/compute/sql",
                    location,
                )
        if kind == "parallel":
            if scope != "main" or parent is not None:
                structure_error("parallel 只能位于 main 根节点", location)
            value = step.get("max_concurrency")
            if type(value) is not int or not 1 <= value <= 8:
                structure_error("max_concurrency 必须为 1～8", location)
            if len(siblings(task, "main", step["id"])) < 2:
                structure_error("parallel 至少包含两个子节点", location)
        if kind == "wait":
            value = step.get("wait_milliseconds")
            dynamic = (step.get("options") or {}).get("wait")
            if not dynamic and (type(value) is not int or not 1 <= value <= MAX_WAIT_MS):
                structure_error(
                    "固定 wait_milliseconds 必须为 1～2592000000，或配置动态等待", location
                )
        if kind != "rest" and REST_FIELDS & step.keys():
            structure_error("非 REST 节点禁止 REST 专用字段", location)
        if kind != "wait" and "wait_milliseconds" in step:
            structure_error("只有 wait 可配置 wait_milliseconds", location)
        if kind != "parallel" and "max_concurrency" in step:
            structure_error("只有 parallel 可配置 max_concurrency", location)
    return by_id


def parameter_sources(task):
    from app.service.task_config import expression_tokens

    steps = {s["id"]: s for s in task["steps"]}
    sources = {}
    for parameter in task["global_parameters"]:
        if parameter["source_type"] in {"fixed", "function"}:
            continue
        if parameter["source_type"] == "expression":
            source = parameter.get("expression_source") or {}
            step = steps.get(source.get("step_id"))
            if set(source) != {"step_id"} or not step or step["step_type"] != "compute":
                structure_error("表达式参数来源必须为本任务 compute 节点", "expression_source")
            sources.setdefault(step["id"], []).append(parameter)
            continue
        if parameter["source_type"] != "rest":
            structure_error("参数来源须为 fixed/function/rest/expression", "source_type")
        source = parameter.get("rest_source") or {}
        if set(source) != {"step_id", "extract_expression"}:
            structure_error("REST 参数必须指定稳定步骤 ID 和提取表达式", "rest_source")
        step = steps.get(source["step_id"])
        if not step or step["step_type"] != "rest":
            structure_error("参数来源只能为本任务 main 或 finally REST 叶子", "rest_source.step_id")
        expression_tokens(source["extract_expression"])
        sources.setdefault(step["id"], []).append(parameter)
    declarations = {p["name"]: p for p in task["global_parameters"]}
    for step in task["steps"]:
        for assignment in (step.get("options") or {}).get("assignments", []):
            original = declarations.get(assignment["name"])
            if not original or any(
                p["name"] == assignment["name"] for p in sources.get(step["id"], [])
            ):
                structure_error("参数更新目标未声明或与本步骤提取重复", "options.assignments")
            parameter = dict(
                original,
                source_type="rest",
                rest_source={
                    "step_id": step["id"],
                    "extract_expression": assignment["extract_expression"],
                },
            )
            sources.setdefault(step["id"], []).append(parameter)
    for step in task["steps"]:
        if step["step_type"] == "parallel":
            names = set()
            for child in siblings(task, parent=step["id"]):
                outputs = {p["name"] for p in sources.get(child["id"], [])}
                if names & outputs:
                    structure_error("并发兄弟不能更新同一参数", "options.assignments")
                names.update(outputs)
    return sources


def empty_stats(rest_total):
    return {
        "rest_total": rest_total,
        "pending_count": rest_total,
        "running_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
        "skipped_count": 0,
        "request_count": 0,
        "response_count": 0,
        "response_time_sum_ms": 0,
        "avg_response_time_ms": None,
        "max_response_time_ms": None,
        "min_response_time_ms": None,
    }
