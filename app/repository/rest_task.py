"""Explicit operation handlers for docs/ rest-api/rest-task.yaml."""

import base64
import copy
import json

from psycopg.types.json import Jsonb

from app.repository.projects import required
from app.service.errors import BusinessError, invalid
from app.service.task_config import safe_task, validate_config

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
            "SELECT id FROM task_runs WHERE task_id=%s AND status IN ('queued','running') LIMIT 1",
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
    return result


def enrich(repository, config):
    sources = {
        p["rest_source"]["step_id"]
        for p in config["global_parameters"]
        if p.get("source_type") == "rest" and p.get("rest_source") and "step_id" in p["rest_source"]
    }
    for s in config["steps"]:
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
            execution_phase="parameters" if s["id"] in sources else "steps",
        )
    by_id = {s["id"]: s for s in config["steps"]}
    for p in config["global_parameters"]:
        if p.get("source_type") == "rest":
            source = p.get("rest_source") or {}
            s = by_id.get(source.get("step_id"))
            if s:
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
    safe_steps = {step["id"]: step for step in safe_task(existing)["steps"]} if existing else {}
    used = set()
    for i, s in enumerate(config["steps"]):
        if "id" in s:
            if s["id"] not in oldsteps or s["id"] in used:
                invalid("步骤 ID 重复或不属于当前任务", "body.steps")
            used.add(s["id"])
            previous = {
                (f["location"], f["name"]): f["binding"] for f in oldsteps[s["id"]]["bindings"]
            }
            for f in s["bindings"]:
                if f["binding"]["kind"] == "literal" and "value" not in f["binding"]:
                    old = previous.get((f["location"], f["name"]))
                    safe_binding = next(
                        (
                            field["binding"]
                            for field in safe_steps[s["id"]]["bindings"]
                            if field["location"] == f["location"] and field["name"] == f["name"]
                        ),
                        {},
                    )
                    if (
                        not old
                        or old["kind"] != "literal"
                        or not safe_binding.get("redacted")
                        or oldsteps[s["id"]]["interface_id"] != s["interface_id"]
                    ):
                        invalid("必须提供字面值", "body.steps.bindings")
                    f["binding"]["value"] = old["value"]
        else:
            s["id"] = -(i + 1)
        s["position"] = i + 1
    for i, p in enumerate(config["global_parameters"]):
        source = p.get("rest_source")
        if p["source_type"] != "rest" or not source or "step_index" not in source:
            continue
        index = source["step_index"]
        if "step_id" in source or type(index) is not int or not 0 <= index < len(config["steps"]):
            invalid(
                "来源步骤下标无效或同时指定了步骤 ID",
                f"body.global_parameters.{i}.rest_source.step_index",
            )
        source.pop("step_index")
        source["step_id"] = config["steps"][index]["id"]
    enrich(repository, config)
    validate_config(config)
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
        values = (
            tid,
            s["position"],
            s["interface_id"],
            s["file_id"],
            s["version"],
            Jsonb(s["bindings"]),
            s.get("assertion_code"),
        )
        if s["id"] < 0:
            s["id"] = repository.one(
                "INSERT INTO task_steps(task_id,position,interface_id,file_id,version,bindings,assertion_code) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                values,
            )["id"]
        else:
            repository.db.execute(
                "UPDATE task_steps SET task_id=%s,position=%s,interface_id=%s,file_id=%s,version=%s,bindings=%s,assertion_code=%s,updated_at=now() WHERE id=%s",
                (*values, s["id"]),
            )
        step_ids[original_id] = s["id"]
    repository.db.execute(
        "DELETE FROM task_steps WHERE task_id=%s AND NOT(id=ANY(%s))",
        (tid, [s["id"] for s in config["steps"]]),
    )
    parameter_ids = []
    for i, p in enumerate(config["global_parameters"]):
        if p["source_type"] == "rest":
            source = p["rest_source"]
            source["step_id"] = step_ids[source["step_id"]]
        values = (
            tid,
            p["name"],
            p["value_type"],
            Jsonb(p["value"]) if p["source_type"] == "fixed" else None,
            p["sensitive"],
            i + 1,
            p["source_type"],
            Jsonb(p["rest_source"]) if p["source_type"] == "rest" else None,
        )
        old = oldparams.get(p["name"])
        if old:
            repository.db.execute(
                "UPDATE task_parameters SET task_id=%s,name=%s,value_type=%s,value=%s,sensitive=%s,position=%s,source_type=%s,rest_source=%s WHERE id=%s",
                (*values, old["id"]),
            )
            parameter_ids.append(old["id"])
        else:
            parameter_ids.append(
                repository.one(
                    "INSERT INTO task_parameters(task_id,name,value_type,value,sensitive,position,source_type,rest_source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    values,
                )["id"]
            )
    repository.db.execute(
        "DELETE FROM task_parameters WHERE task_id=%s AND NOT(id=ANY(%s))", (tid, parameter_ids)
    )
    return safe_task(configuration(repository, dict(path, task_id=tid)))


def listTasks(repository, path, query, body, upload):
    repository.project(path["project_id"])
    return repository.page(
        "SELECT t.*, (SELECT count(*) FROM task_steps WHERE task_id=t.id) AS step_count, EXISTS(SELECT 1 FROM task_runs WHERE task_id=t.id AND status IN ('queued','running')) AS has_active_run FROM tasks t WHERE project_id=%s AND strpos(lower(name),lower(%s))>0 ORDER BY updated_at DESC,id",
        (path["project_id"], query.get("name", "")),
        query,
    )


def importTask(repository, path, query, body, upload):
    from app.service.task_import import convert

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

    config = convert(upload, body["base_url"], lookup)
    return save(repository, path, config, creating=True)


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
    row = repository.one(
        "INSERT INTO tasks(project_id,name,description,base_url,timeout_seconds) VALUES(%s,%s,%s,%s,%s) RETURNING id",
        (
            path["project_id"],
            body["name"],
            original["description"],
            original["base_url"],
            original["timeout_seconds"],
        ),
    )
    mapping = {}
    for step in original["steps"]:
        inserted = repository.one(
            "INSERT INTO task_steps(task_id,position,interface_id,file_id,version,bindings,assertion_code) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                row["id"],
                step["position"],
                step["interface_id"],
                step["file_id"],
                step["version"],
                Jsonb(step["bindings"]),
                step.get("assertion_code"),
            ),
        )
        mapping[step["id"]] = inserted["id"]
    payload = {
        key: copy.deepcopy(original[key])
        for key in [
            "name",
            "description",
            "base_url",
            "timeout_seconds",
            "global_parameters",
            "steps",
        ]
    }
    payload["name"] = body["name"]
    for step in payload["steps"]:
        step["id"] = mapping[step["id"]]
    for parameter in payload["global_parameters"]:
        if parameter["source_type"] == "rest":
            parameter["rest_source"]["step_id"] = mapping[parameter["rest_source"]["step_id"]]
    return save(repository, dict(path, task_id=row["id"]), payload)


def previewTaskDeletion(repository, path, query, body, upload):
    row = task(repository, path)
    tid = row["id"]
    blocked = active(repository, tid)
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
        blocking_reasons=["存在未结束运行"] if blocked else [],
    )


def delete_task_rows(repository, tid):
    if active(repository, tid):
        raise BusinessError(409, "TASK_ACTIVE_RUN", "存在未结束运行，不能删除")
    for table in ["task_requests", "task_run_parameters", "task_run_steps"]:
        repository.db.execute(
            f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM task_runs WHERE task_id=%s)",
            (tid,),
        )
    for table in ["task_runs", "task_steps", "task_parameters"]:
        repository.db.execute(f"DELETE FROM {table} WHERE task_id=%s", (tid,))
    repository.db.execute("DELETE FROM tasks WHERE id=%s", (tid,))


def deleteTask(repository, path, query, body, upload):
    task(repository, path, True)
    delete_task_rows(repository, path["task_id"])
    return {"id": path["task_id"], "deleted": True}


def validateTask(repository, path, query, body, upload):
    config = configuration(repository, path)
    try:
        validate_config(config, body.get("mode", "run") == "run")
    except BusinessError as exc:
        return {"valid": False, "issues": exc.payload["issues"]}
    return {"valid": True, "issues": []}


def startTaskRun(repository, path, query, body, upload):
    task(repository, path, True)
    if active(repository, path["task_id"]):
        raise BusinessError(409, "TASK_ACTIVE_RUN", "任务存在未结束运行")
    config = configuration(repository, path)
    validate_config(config, True)
    from fastapi.encoders import jsonable_encoder

    config = jsonable_encoder(config)
    row = repository.one(
        "INSERT INTO task_runs(task_id,project_id,config_snapshot) VALUES(%s,%s,%s) RETURNING *",
        (path["task_id"], path["project_id"], Jsonb(config)),
    )
    for s in config["steps"]:
        repository.db.execute(
            "INSERT INTO task_run_steps(run_id,step_id,position) VALUES(%s,%s,%s)",
            (row["id"], s["id"], s["position"]),
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
    return row


def run(repository, path):
    task(repository, path)
    return required(
        repository.one(
            "SELECT * FROM task_runs WHERE task_id=%s AND id=%s", (path["task_id"], path["run_id"])
        )
    )


def listTaskRuns(repository, path, query, body, upload):
    task(repository, path)
    return repository.page(
        "SELECT id,task_id,project_id,status,created_at,started_at,finished_at,duration_ms,error_code,error_message FROM task_runs WHERE task_id=%s AND (%s='' OR status=%s) ORDER BY created_at DESC,id DESC",
        (path["task_id"], query.get("status", ""), query.get("status", "")),
        query,
    )


def getTaskRun(repository, path, query, body, upload):
    from app.service.task_config import scrub, sensitive_values

    row = run(repository, path)
    config = row.pop("config_snapshot")
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
        "interface_definitions": [scrub(redact(s["interface"]), secrets) for s in config["steps"]],
        "redacted_paths": [
            "/task/global_parameters/" + str(i) + "/value"
            for i, p in enumerate(config["global_parameters"])
            if p["sensitive"]
        ],
    }
    for parameter in parameters:
        if "value" in parameter:
            parameter["value"] = scrub(parameter["value"], secrets)
    return {
        "run": row,
        "config_snapshot": snapshot,
        "steps": steps,
        "global_parameters": parameters,
    }


def safe_request(repository, row, config):
    from app.service.task_config import SECRET_HEADERS, scrub, sensitive_values

    values = {
        p["name"]: p["value"]
        for p in repository.db.execute(
            "SELECT name,value FROM task_run_parameters WHERE run_id=%s AND status='succeeded'",
            (row["run_id"],),
        )
    }
    secrets = sensitive_values(config, values)
    for key in ["request_headers", "response_headers"]:
        if row[key] is not None:
            for h in row[key]:
                h["redacted"] = h["name"].lower() in SECRET_HEADERS
                if h["redacted"]:
                    secrets.append(h["value"])
                    h["value"] = "***"
    row.pop("redaction_metadata", None)
    original = copy.deepcopy(row)
    source_sensitive = any(
        p.get("sensitive")
        and p.get("source_type") == "rest"
        and p.get("rest_source", {}).get("step_id") == row["step_id"]
        for p in config["global_parameters"]
    )
    if source_sensitive and row["response_body"] is not None:
        # Covers values that failed extraction/type validation and were never persisted.
        row["response_body"] = "Kioq" if row["response_body_encoding"] == "base64" else "***"
    elif row["response_body_encoding"] == "base64" and secrets and row["response_body"]:
        row["response_body"] = base64.b64encode(b"***").decode("ascii")
    else:
        from app.service.openapi_import import redact

        for key in ["request_body", "response_body"]:
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
        "SELECT * FROM task_requests WHERE run_id=%s ORDER BY CASE phase WHEN 'parameters' THEN 0 ELSE 1 END,position,id",
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
