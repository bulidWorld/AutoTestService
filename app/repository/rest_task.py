"""Explicit operation handlers for docs/ rest-api/rest-task.yaml."""

import base64
import copy
import json

from psycopg.types.json import Jsonb

from app.repository.projects import required
from app.service.errors import BusinessError, invalid
from app.service.task_config import safe_task, validate_config
from app.service.task_nodes import REST_FIELDS, empty_stats, preorder, siblings

SYSTEM_OPERATIONS = set()


def task(repository, path, lock=False):
    repository.project(path["project_id"], lock=lock)
    return required(
        repository.one(
            "SELECT * FROM tasks WHERE project_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
            (path["project_id"], path["task_id"]),
        )
    )


def active(repository, tid):
    return (
        repository.one(
            "SELECT id FROM task_runs WHERE task_id=%s AND status IN ('queued','running') "
            "UNION ALL SELECT r.id FROM task_collection_run_members m "
            "JOIN task_collection_runs r ON r.id=m.collection_run_id "
            "WHERE m.task_id=%s AND r.status IN ('queued','running') LIMIT 1",
            (tid, tid),
        )
        is not None
    )


def reserved(repository, tid):
    return (
        repository.one(
            "SELECT r.id FROM task_collection_run_members m "
            "JOIN task_collection_runs r ON r.id=m.collection_run_id "
            "WHERE m.task_id=%s AND r.status IN ('queued','running') LIMIT 1",
            (tid,),
        )
        is not None
    )


def referenced(repository, tid):
    return (
        repository.one(
            "SELECT task_id FROM task_collection_members WHERE task_id=%s LIMIT 1",
            (tid,),
        )
        is not None
    )


def configuration(repository, path):
    result = task(repository, path)
    tid = result["id"]
    params = repository.db.execute(
        "SELECT * FROM task_parameters WHERE task_id=%s ORDER BY position,id", (tid,)
    ).fetchall()
    steps = repository.db.execute(
        "SELECT * FROM task_steps WHERE task_id=%s ORDER BY position,id", (tid,)
    ).fetchall()
    result["global_parameters"], result["steps"] = params, steps
    enrich(repository, result)
    result["steps"] = list(preorder(result))
    return result


def enrich(repository, config):
    sources = {
        p["rest_source"]["step_id"]
        for p in config["global_parameters"]
        if p.get("source_type") == "rest" and p.get("rest_source") and "step_id" in p["rest_source"]
    }
    for s in config["steps"]:
        for key in ("wait_milliseconds", "max_concurrency", "raw_body"):
            if s.get(key) is None:
                s.pop(key, None)
        if s["step_type"] != "rest":
            for key in REST_FIELDS:
                s.pop(key, None)
            continue
        f = required(
            repository.one(
                "SELECT * FROM yaml_files WHERE id=%s AND project_id=%s",
                (s["file_id"], config["project_id"]),
            )
        )
        interface = required(
            repository.one(
                "SELECT * FROM api_interfaces WHERE id=%s AND file_id=%s AND version=%s",
                (s["interface_id"], s["file_id"], s["version"]),
            )
        )
        version = required(
            repository.one(
                "SELECT parsed_document FROM yaml_versions WHERE file_id=%s AND version=%s",
                (s["file_id"], s["version"]),
            )
        )
        s.update(
            interface=interface,
            document=version["parsed_document"],
            file_name=f["name"],
            current_version=f["current_version"],
            has_new_version=f["current_version"] > s["version"],
            method=interface["method"],
            path=interface["path"],
            execution_phase="finally"
            if s["execution_scope"] == "finally"
            else ("parameters" if s["id"] in sources else "steps"),
        )
    by_id = {s["id"]: s for s in config["steps"]}
    for p in config["global_parameters"]:
        if p.get("source_type") == "expression":
            p.pop("value", None)
            p.pop("rest_source", None)
            continue
        p.pop("expression_source", None)
        if p.get("source_type") == "rest":
            source = p.get("rest_source") or {}
            s = by_id.get(source.get("step_id"))
            if s and s["step_type"] == "rest":
                p.update(
                    interface_name=s["interface"]["summary"] or s["method"] + " " + s["path"],
                    file_name=s["file_name"],
                    current_version=s["current_version"],
                    has_new_version=s["has_new_version"],
                )
            p.pop("value", None)
        else:
            p.pop("rest_source", None)
    return config


def task_name(repository, pid, name, tid=None):
    if not name.strip():
        invalid("任务名称不得为空", "body.name")
    if repository.one(
        "SELECT id FROM tasks WHERE project_id=%s AND name=%s AND (%s::integer IS NULL OR id<>%s)",
        (pid, name, tid, tid),
    ):
        raise BusinessError(409, "TASK_NAME_CONFLICT", "同项目任务名称已存在", "body.name")


def save(repository, path, body, creating=False):
    pid = path["project_id"]
    repository.project(pid, lock=True)
    existing = None if creating else configuration(repository, path)
    if existing:
        task(repository, path, lock=True)
        if active(repository, existing["id"]):
            raise BusinessError(409, "TASK_ACTIVE_RUN", "任务存在未结束运行或已被任务组预约")
    config = copy.deepcopy(body)
    config["project_id"] = pid
    config["name"] = config["name"].strip()
    task_name(repository, pid, config["name"], existing["id"] if existing else None)
    oldparams = {p["name"]: p for p in existing["global_parameters"]} if existing else {}
    oldsteps = {s["id"]: s for s in existing["steps"]} if existing else {}
    for p in config["global_parameters"]:
        p["name"] = p["name"].strip()
        p.setdefault("source_type", "fixed")
        p.setdefault("sensitive", False)
        old = oldparams.get(p["name"])
        if p["source_type"] == "fixed" and "value" not in p:
            if (
                not old
                or old["source_type"] != "fixed"
                or not old["sensitive"]
                or old["value_type"] != p["value_type"]
            ):
                invalid("必须提供固定参数值", "body.global_parameters")
            p["value"] = old["value"]
    safe_steps = {s["id"]: s for s in safe_task(existing)["steps"]} if existing else {}
    used = set()
    for i, s in enumerate(config["steps"]):
        if "parent_step_index" in s and "parent_step_id" in s:
            invalid("父 ID 与父下标只能指定一个", f"body.steps.{i}.parent_step_index")
        s.setdefault("parent_step_id", None)
        if "id" in s:
            if s["id"] not in oldsteps or s["id"] in used:
                invalid("步骤 ID 重复或不属于当前任务", f"body.steps.{i}.id")
            used.add(s["id"])
            oldstep = oldsteps[s["id"]]
            for container in ("bindings", "headers"):
                oldfields = {
                    (
                        f.get("location"),
                        f["name"].lower() if container == "headers" else f["name"],
                    ): f["binding"]
                    for f in oldstep.get(container, [])
                }
                safefields = {
                    (
                        f.get("location"),
                        f["name"].lower() if container == "headers" else f["name"],
                    ): f["binding"]
                    for f in safe_steps[s["id"]].get(container, [])
                }
                for f in s.get(container, []):
                    binding = f["binding"]
                    if binding["kind"] == "literal" and "value" not in binding:
                        key = (
                            f.get("location"),
                            f["name"].lower() if container == "headers" else f["name"],
                        )
                        previous = oldfields.get(key, {})
                        if (
                            not safefields.get(key, {}).get("redacted")
                            or previous.get("kind") != "literal"
                            or s.get("interface_id") != oldstep.get("interface_id")
                        ):
                            invalid("必须提供字面值", f"body.steps.{i}.{container}")
                        binding["value"] = previous["value"]
        else:
            s["id"] = -(i + 1)
        if s.get("step_type") == "rest":
            s.setdefault("headers", [])
    for i, s in enumerate(config["steps"]):
        if "parent_step_index" not in s:
            continue
        index = s.pop("parent_step_index")
        if (
            s["parent_step_id"] is not None
            or type(index) is not int
            or not 0 <= index < len(config["steps"])
        ):
            invalid("父下标无效或同时指定父 ID", f"body.steps.{i}.parent_step_index")
        s["parent_step_id"] = config["steps"][index]["id"]
    for i, p in enumerate(config["global_parameters"]):
        field = "expression_source" if p["source_type"] == "expression" else "rest_source"
        source = p.get(field)
        if (
            p["source_type"] not in {"rest", "expression"}
            or not source
            or "step_index" not in source
        ):
            continue
        index = source["step_index"]
        if "step_id" in source or type(index) is not int or not 0 <= index < len(config["steps"]):
            invalid(
                "来源步骤下标无效或同时指定了步骤 ID",
                f"body.global_parameters.{i}.{field}.step_index",
            )
        source.pop("step_index")
        source["step_id"] = config["steps"][index]["id"]
    enrich(repository, config)
    validate_config(config, allow_collection_inputs=True)
    if creating:
        row = repository.one(
            "INSERT INTO tasks(project_id,name,description,base_url,timeout_seconds) VALUES(%s,%s,%s,%s,%s) RETURNING *",
            (
                pid,
                config["name"],
                config.get("description", ""),
                config["base_url"],
                config.get("timeout_seconds", 30),
            ),
        )
    else:
        row = repository.one(
            "UPDATE tasks SET name=%s,description=%s,base_url=%s,timeout_seconds=%s,updated_at=now() WHERE id=%s RETURNING *",
            (
                config["name"],
                config.get("description", ""),
                config["base_url"],
                config.get("timeout_seconds", 30),
                existing["id"],
            ),
        )
    tid = row["id"]
    step_ids = {}
    for s in config["steps"]:
        original_id = s["id"]
        columns = [
            "task_id",
            "position",
            "step_type",
            "execution_scope",
            "interface_id",
            "file_id",
            "version",
            "bindings",
            "headers",
            "raw_body",
            "assertion_code",
            "wait_milliseconds",
            "max_concurrency",
            "options",
        ]
        values = [
            tid,
            s["position"],
            s["step_type"],
            s["execution_scope"],
            s.get("interface_id"),
            s.get("file_id"),
            s.get("version"),
            Jsonb(s.get("bindings", [])),
            Jsonb(s.get("headers", [])),
            Jsonb(s["raw_body"]) if "raw_body" in s else None,
            s.get("assertion_code"),
            s.get("wait_milliseconds"),
            s.get("max_concurrency"),
            Jsonb(s.get("options") or {}),
        ]
        if original_id < 0:
            s["id"] = repository.one(
                "INSERT INTO task_steps("
                + ",".join(columns)
                + ") VALUES("
                + ",".join(["%s"] * len(columns))
                + ") RETURNING id",
                values,
            )["id"]
        else:
            repository.db.execute(
                "UPDATE task_steps SET "
                + ",".join(c + "=%s" for c in columns)
                + ",updated_at=now() WHERE id=%s",
                (*values, original_id),
            )
        step_ids[original_id] = s["id"]
    for s in config["steps"]:
        parent = s.get("parent_step_id")
        repository.db.execute(
            "UPDATE task_steps SET parent_step_id=%s WHERE id=%s",
            (step_ids[parent] if parent is not None else None, s["id"]),
        )
    repository.db.execute(
        "DELETE FROM task_steps WHERE task_id=%s AND NOT(id=ANY(%s))",
        (tid, [s["id"] for s in config["steps"]]),
    )
    parameter_ids = []
    for i, p in enumerate(config["global_parameters"]):
        if p["source_type"] in {"rest", "expression"}:
            source = p["expression_source" if p["source_type"] == "expression" else "rest_source"]
            source["step_id"] = step_ids[source["step_id"]]
        values = (
            tid,
            p["name"],
            p["value_type"],
            Jsonb(p["value"]) if p["source_type"] in {"fixed", "function"} else None,
            p["sensitive"],
            i + 1,
            p["source_type"],
            Jsonb(p["rest_source"]) if p["source_type"] == "rest" else None,
            Jsonb(p["expression_source"]) if p["source_type"] == "expression" else None,
        )
        old = oldparams.get(p["name"])
        if old:
            repository.db.execute(
                "UPDATE task_parameters SET task_id=%s,name=%s,value_type=%s,value=%s,sensitive=%s,position=%s,source_type=%s,rest_source=%s,expression_source=%s WHERE id=%s",
                (*values, old["id"]),
            )
            parameter_ids.append(old["id"])
        else:
            parameter_ids.append(
                repository.one(
                    "INSERT INTO task_parameters(task_id,name,value_type,value,sensitive,position,source_type,rest_source,expression_source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    values,
                )["id"]
            )
    repository.db.execute(
        "DELETE FROM task_parameters WHERE task_id=%s AND NOT(id=ANY(%s))", (tid, parameter_ids)
    )
    result = safe_task(configuration(repository, dict(path, task_id=tid)))
    return result


def listTasks(repository, path, query, body, upload):
    repository.project(path["project_id"])
    result = query.get("last_execution_result", "")
    return repository.page(
        "SELECT t.*, (SELECT count(*) FROM task_steps WHERE task_id=t.id) AS step_count, "
        "(EXISTS(SELECT 1 FROM task_runs WHERE task_id=t.id AND status IN ('queued','running')) "
        "OR EXISTS(SELECT 1 FROM task_collection_run_members m "
        "JOIN task_collection_runs c ON c.id=m.collection_run_id "
        "WHERE m.task_id=t.id AND c.status IN ('queued','running'))) AS has_active_run, "
        "coalesce(last_run.result,'not_executed') AS last_execution_result, "
        "coalesce(last_run.started_at,last_run.created_at) AS last_execution_time "
        "FROM tasks t LEFT JOIN LATERAL ("
        "SELECT CASE WHEN status IN ('queued','running') THEN 'queued' ELSE status END AS result,"
        "started_at,created_at FROM task_runs WHERE task_id=t.id "
        "ORDER BY created_at DESC,id DESC LIMIT 1"
        ") last_run ON true "
        "WHERE t.project_id=%s AND strpos(lower(t.name),lower(%s))>0 "
        "AND (%s='' OR coalesce(last_run.result,'not_executed')=%s) "
        "ORDER BY CASE coalesce(last_run.result,'not_executed') "
        "WHEN 'failed' THEN 0 WHEN 'not_executed' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,"
        "t.updated_at DESC,t.id",
        (path["project_id"], query.get("name", ""), result, result),
        query,
    )


def importTask(repository, path, query, body, upload):
    from app.service.task_import import convert, file_issue

    repository.project(path["project_id"], lock=True)

    def lookup(method, interface_path):
        rows = repository.db.execute(
            "SELECT i.* FROM api_interfaces i JOIN yaml_files f ON f.id=i.file_id "
            "WHERE f.project_id=%s AND i.version=f.current_version AND i.method=%s AND i.path=%s",
            (path["project_id"], method, interface_path),
        ).fetchall()
        if len(rows) != 1:
            invalid("XML 接口不存在或匹配多个文件的当前版本", "body.file")
        interface = rows[0]
        version = required(
            repository.one(
                "SELECT parsed_document FROM yaml_versions WHERE file_id=%s AND version=%s",
                (interface["file_id"], interface["version"]),
            )
        )
        return interface, version["parsed_document"]

    tasks = []
    for index, (filename, root) in enumerate(upload):
        try:
            tasks.append(
                save(repository, path, convert(root, body["base_url"], lookup), creating=True)
            )
        except BusinessError as exc:
            import re

            for issue in exc.payload["issues"]:
                match = re.search(r"body.steps\.(\d+)", issue["location"])
                if match:
                    elements = []

                    def collect(container, elements=elements):
                        for node in sorted(
                            container.findall("taskStep"), key=lambda n: int(n.get("order"))
                        ):
                            elements.append(node)
                            parallel = node.find("parallel")
                            if parallel is not None:
                                collect(parallel)

                    collect(root)
                    cleanup = root.find("finally")
                    if cleanup is not None:
                        collect(cleanup)
                    node = elements[int(match.group(1))]
                    issue["location"] = (
                        root.getroottree().getpath(node) + f" (line {node.sourceline})"
                    )
            raise file_issue(index, filename, exc) from None
    return tasks


def createTask(repository, path, query, body, upload):
    return save(repository, path, body, True)


def getTask(repository, path, query, body, upload):
    return safe_task(configuration(repository, path))


def updateTask(repository, path, query, body, upload):
    return save(repository, path, body)


def copyTask(repository, path, query, body, upload):
    repository.project(path["project_id"], lock=True)
    original = configuration(repository, path)
    task_name(repository, path["project_id"], body["name"])
    payload = {
        key: copy.deepcopy(original[key])
        for key in (
            "name",
            "description",
            "base_url",
            "timeout_seconds",
            "global_parameters",
            "steps",
        )
    }
    payload["name"] = body["name"]
    indices = {s["id"]: i for i, s in enumerate(payload["steps"])}
    for step in payload["steps"]:
        step.pop("id")
        parent = step.pop("parent_step_id", None)
        if parent is not None:
            step["parent_step_index"] = indices[parent]
    for parameter in payload["global_parameters"]:
        if parameter["source_type"] in {"rest", "expression"}:
            source = parameter[
                "expression_source" if parameter["source_type"] == "expression" else "rest_source"
            ]
            source["step_index"] = indices[source.pop("step_id")]
    return save(repository, path, payload, creating=True)


def previewTaskDeletion(repository, path, query, body, upload):
    row = task(repository, path)
    tid = row["id"]
    blocked = active(repository, tid) or referenced(repository, tid)
    counts = {}
    for key, sql in [
        ("step_count", "SELECT count(*) AS n FROM task_steps WHERE task_id=%s"),
        ("run_count", "SELECT count(*) AS n FROM task_runs WHERE task_id=%s"),
        (
            "request_count",
            "SELECT count(*) AS n FROM task_requests q JOIN task_runs r ON r.id=q.run_id WHERE r.task_id=%s",
        ),
    ]:
        counts[key] = repository.one(sql, (tid,))["n"]
    return dict(
        task_id=tid,
        **counts,
        can_delete=not blocked,
        blocking_reasons=["存在未结束运行或被任务组引用"] if blocked else [],
    )


def delete_task_rows(repository, tid):
    if active(repository, tid):
        raise BusinessError(409, "TASK_ACTIVE_RUN", "存在未结束运行，不能删除")
    retained = "SELECT task_run_id FROM task_collection_run_members"
    for table in ["task_requests", "task_run_parameters", "task_run_steps"]:
        repository.db.execute(
            f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM task_runs WHERE task_id=%s "
            f"AND id NOT IN ({retained}))",
            (tid,),
        )
    for table in ["task_steps", "task_parameters"]:
        repository.db.execute(f"DELETE FROM {table} WHERE task_id=%s", (tid,))
    repository.db.execute(
        f"DELETE FROM task_runs WHERE task_id=%s AND id NOT IN ({retained})",
        (tid,),
    )
    repository.db.execute("DELETE FROM tasks WHERE id=%s", (tid,))


def deleteTask(repository, path, query, body, upload):
    task(repository, path, True)
    if referenced(repository, path["task_id"]):
        raise BusinessError(409, "TASK_REFERENCED", "任务被任务组引用，请先移除成员")
    delete_task_rows(repository, path["task_id"])
    return {"id": path["task_id"], "deleted": True}


def validateTask(repository, path, query, body, upload):
    config = configuration(repository, path)
    try:
        run_mode = body.get("mode", "run") == "run"
        validate_config(config, run_mode, allow_collection_inputs=not run_mode)
    except BusinessError as exc:
        return {"valid": False, "issues": exc.payload["issues"]}
    return {"valid": True, "issues": []}


def startTaskRun(repository, path, query, body, upload):
    task(repository, path, True)
    if active(repository, path["task_id"]):
        raise BusinessError(409, "TASK_ACTIVE_RUN", "任务存在未结束运行")
    config = configuration(repository, path)
    return create_run(repository, path, config)


def create_run(repository, path, config):
    validate_config(config, True)
    from fastapi.encoders import jsonable_encoder

    config = jsonable_encoder(config)
    row = repository.one(
        "INSERT INTO task_runs(task_id,project_id,config_snapshot) VALUES(%s,%s,%s) RETURNING *",
        (path["task_id"], path["project_id"], Jsonb(config)),
    )
    runtime_ids = {}
    for s in config["steps"]:
        stats = (
            empty_stats(sum(c["step_type"] == "rest" for c in siblings(config, parent=s["id"])))
            if s["step_type"] == "parallel"
            else None
        )
        node = repository.one(
            "INSERT INTO task_run_steps(run_id,step_id,position,step_type,execution_scope,parallel_stats) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                row["id"],
                s["id"],
                s["position"],
                s["step_type"],
                s["execution_scope"],
                Jsonb(stats) if stats is not None else None,
            ),
        )
        runtime_ids[s["id"]] = node["id"]
    for s in config["steps"]:
        if s.get("parent_step_id") is not None:
            repository.db.execute(
                "UPDATE task_run_steps SET parent_run_step_id=%s WHERE id=%s",
                (runtime_ids[s["parent_step_id"]], runtime_ids[s["id"]]),
            )
    for p in config["global_parameters"]:
        repository.db.execute(
            "INSERT INTO task_run_parameters(run_id,parameter_id,name,position,source_type,value_type,sensitive,status) VALUES(%s,%s,%s,%s,%s,%s,%s,'pending')",
            (
                row["id"],
                p["id"],
                p["name"],
                p["position"],
                p["source_type"],
                p["value_type"],
                p["sensitive"],
            ),
        )
    row.pop("config_snapshot")
    row.pop("group_id", None)
    return public_run(row)


def run(repository, path):
    repository.project(path["project_id"])
    return required(
        repository.one(
            "SELECT * FROM task_runs WHERE task_id=%s AND id=%s AND project_id=%s",
            (path["task_id"], path["run_id"], path["project_id"]),
        )
    )


def public_run(row):
    code = row.pop("primary_error_code", None)
    message = row.pop("primary_error_message", None)
    row["primary_error"] = {"code": code, "message": message} if code else None
    return row


def listTaskRuns(repository, path, query, body, upload):
    task(repository, path)
    page = repository.page(
        "SELECT id,task_id,project_id,status,execution_stage,cleanup_status,primary_error_code,primary_error_message,created_at,started_at,finished_at,duration_ms,error_code,error_message FROM task_runs WHERE task_id=%s AND (%s='' OR status=%s) ORDER BY created_at DESC,id DESC",
        (path["task_id"], query.get("status", ""), query.get("status", "")),
        query,
    )
    page["items"] = [public_run(row) for row in page["items"]]
    return page


def getTaskRun(repository, path, query, body, upload):
    from app.service.task_config import scrub, sensitive_values

    row = run(repository, path)
    row.pop("group_id", None)
    config = run_secrets(repository, row.pop("config_snapshot"), row["id"])
    parameters = repository.db.execute(
        "SELECT * FROM task_run_parameters WHERE run_id=%s ORDER BY position,id", (row["id"],)
    ).fetchall()
    values = {p["name"]: p["value"] for p in parameters if p["status"] == "succeeded"}
    secrets = sensitive_values(config, values)
    for p in parameters:
        p.pop("run_id", None)
        p["redacted"] = bool(p["sensitive"] and p["status"] == "succeeded")
        if p["redacted"] or p["status"] != "succeeded":
            p.pop("value", None)
    steps = repository.db.execute(
        "SELECT s.*,q.id AS request_id FROM task_run_steps s LEFT JOIN task_requests q ON q.run_step_id=s.id WHERE s.run_id=%s ORDER BY s.position,s.id",
        (row["id"],),
    ).fetchall()
    from app.service.openapi_import import redact

    snapshot = {
        "task": safe_task(config),
        "interface_definitions": [
            scrub(redact(s["interface"]), secrets)
            for s in config["steps"]
            if s["step_type"] == "rest"
        ],
        "redacted_paths": [
            "/task/global_parameters/" + str(i) + "/value"
            for i, p in enumerate(config["global_parameters"])
            if p["sensitive"]
        ],
    }
    for parameter in parameters:
        if "value" in parameter:
            parameter["value"] = scrub(parameter["value"], secrets)
    counts = {
        key: {
            "total": 0,
            "pending": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "blocked": 0,
            "skipped": 0,
        }
        for key in ("leaf_counts", "control_counts")
    }
    by_step = {s["step_id"]: s for s in steps}
    request_count = repository.one(
        "SELECT count(*) AS n FROM task_requests WHERE run_id=%s", (row["id"],)
    )["n"]
    for node in steps:
        group = counts["control_counts" if node["step_type"] == "parallel" else "leaf_counts"]
        group["total"] += 1
        group[node["status"]] += 1
        if node["step_type"] == "parallel":
            request_count += (node["parallel_stats"] or {}).get("request_count", 0)
    visible = []
    for node in preorder(config):
        result = by_step[node["id"]]
        if result["parent_run_step_id"] is not None:
            continue
        result.pop("run_id", None)
        result.pop("input_snapshot", None)
        if result["step_type"] != "parallel":
            result.pop("parallel_stats", None)
        else:
            failures = sorted(
                (
                    s
                    for s in steps
                    if s["parent_run_step_id"] == result["id"]
                    and s["status"] in {"failed", "blocked"}
                ),
                key=lambda s: (s["position"], s["id"]),
            )
            result["failed_child_step_id"] = failures[0]["step_id"] if failures else None
            result["child_error_code"] = failures[0]["error_code"] if failures else None
            result["child_error_message"] = failures[0]["error_message"] if failures else None
            result["failed_child_run_uuid"] = failures[0]["run_uuid"] if failures else None
        visible.append(result)
    return {
        "run": public_run(row),
        "config_snapshot": snapshot,
        "steps": visible,
        "global_parameters": parameters,
        **counts,
        "request_count": request_count,
    }


def run_secrets(repository, config, rid):
    if "collection_run_id" not in config:
        return config
    config = copy.deepcopy(config)
    member = repository.one(
        "SELECT input_parameters FROM task_collection_run_members WHERE task_run_id=%s",
        (rid,),
    )
    inherited = member["input_parameters"] if member else {}
    config["collection_secrets"] = [p["value"] for p in inherited.values() if p["sensitive"]]
    return config


def safe_request(repository, row, config):
    from app.service.task_config import SECRET_HEADERS, scrub, sensitive_values

    values = {
        p["name"]: p["value"]
        for p in repository.db.execute(
            "SELECT name,value FROM task_run_parameters WHERE run_id=%s AND status='succeeded'",
            (row["run_id"],),
        )
    }
    config = run_secrets(repository, config, row["run_id"])
    secrets = sensitive_values(config, values)
    for key in ["request_headers", "response_headers"]:
        if row[key] is not None:
            for h in row[key]:
                h["redacted"] = h["name"].lower() in SECRET_HEADERS
                if h["redacted"]:
                    secrets.append(h["value"])
                    h["value"] = "***"
    raw_step = next((s for s in config["steps"] if s["id"] == row["step_id"]), {})
    raw = "raw_body" in raw_step
    row.pop("run_parameter_id", None)
    row.pop("redaction_metadata", None)
    original = copy.deepcopy(row)
    from app.service.task_nodes import parameter_sources

    source_sensitive = any(
        p.get("sensitive") for p in parameter_sources(config).get(row["step_id"], [])
    )
    if source_sensitive and row["response_body"] is not None:
        # Covers values that failed extraction/type validation and were never persisted.
        row["response_body"] = "Kioq" if row["response_body_encoding"] == "base64" else "***"
    elif row["response_body_encoding"] == "base64" and secrets and row["response_body"]:
        row["response_body"] = base64.b64encode(b"***").decode("ascii")
    else:
        from app.service.openapi_import import redact

        for key in ["response_body"] if raw else ["request_body", "response_body"]:
            if row[key]:
                try:
                    parsed = json.loads(row[key])
                    safe = redact(parsed)
                    if safe != parsed:
                        row[key] = json.dumps(safe, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
    result = copy.deepcopy(row)
    for key in [
        "actual_url",
        "request_body",
        "response_body",
        "error_message",
        "request_headers",
        "response_headers",
    ]:
        if key != "request_body" or not raw:
            result[key] = scrub(result[key], secrets)
    for key in ["request_headers", "response_headers"]:
        if result[key] is not None:
            for old, safe in zip(row[key], result[key]):
                safe["redacted"] = safe["redacted"] or safe["value"] != old["value"]
    result["redacted"] = result != original or any(
        h["redacted"] for k in ["request_headers", "response_headers"] for h in result.get(k) or []
    )
    return result


def listRunRequests(repository, path, query, body, upload):
    r = run(repository, path)
    result = repository.page(
        "SELECT * FROM task_requests WHERE run_id=%s ORDER BY CASE phase WHEN 'parameters' THEN 0 WHEN 'steps' THEN 1 ELSE 2 END,position,id",
        (r["id"],),
        query,
    )
    result["items"] = [safe_request(repository, q, r["config_snapshot"]) for q in result["items"]]
    return result


def getRunRequest(repository, path, query, body, upload):
    r = run(repository, path)
    q = required(
        repository.one(
            "SELECT * FROM task_requests WHERE run_id=%s AND id=%s", (r["id"], path["request_id"])
        )
    )
    return safe_request(repository, q, r["config_snapshot"])


def previewRestParameter(repository, path, query, body, upload):
    from app.service.task_execution import preview

    original = configuration(repository, path)
    config = copy.deepcopy(body)
    config["project_id"] = path["project_id"]
    allowed = {s["id"] for s in original["steps"]}
    if any(s.get("id") not in allowed for s in config["steps"]):
        invalid("预览步骤必须属于当前任务", "body.steps")
    enrich(repository, config)
    validate_config(config)
    # Release read transaction before potentially slow HTTP calls.
    repository.db.commit()
    return preview(config, body["parameter_name"])


OPERATIONS = {
    f.__name__: f
    for f in [
        listTasks,
        createTask,
        importTask,
        getTask,
        updateTask,
        copyTask,
        previewTaskDeletion,
        deleteTask,
        validateTask,
        startTaskRun,
        listTaskRuns,
        getTaskRun,
        listRunRequests,
        getRunRequest,
        previewRestParameter,
    ]
}
