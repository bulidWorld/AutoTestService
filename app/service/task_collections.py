"""Collection dependency checks, parameter provenance, batch scheduling and durable audit."""

import ast
import copy
import logging
from itertools import groupby

from psycopg.types.json import Jsonb

from app.service.errors import invalid
from app.service.task_config import build_request, execution_plan, validate_config

LOGGER = logging.getLogger(__name__)


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


def validate_members(members):
    members.sort(key=lambda m: (m["position"], m["config"].get("id", 0)))
    types, sensitive = {}, {}
    for member in members:
        for p in member["config"]["global_parameters"]:
            if p["name"] in types and types[p["name"]] != p["value_type"]:
                invalid("任务组同名参数类型不一致", "members.global_parameters")
            types[p["name"]] = p["value_type"]
            sensitive[p["name"]] = sensitive.get(p["name"], False) or p["sensitive"]
    available = set()
    for _, batch in groupby(members, key=lambda m: m["position"]):
        outputs = set()
        for member in batch:
            config = member["config"]
            config["collection_available"] = sorted(available)
            for p in config["global_parameters"]:
                p["sensitive"] = sensitive[p["name"]]
            validate_config(config, run=True)
            declarations = {p["name"]: p for p in config["global_parameters"]}
            ready = {n: declarations[n] for n in available & declarations.keys()}
            for kind, item in execution_plan(config):
                if kind == "parameter":
                    ready[item["name"]] = item
                else:
                    if (
                        assertion_parameters(item.get("assertion_code"), declarations)
                        - ready.keys()
                    ):
                        invalid("断言引用未声明或尚未赋值的参数", "members.assertion_code")
                    build_request(config, item, declarations=ready)
            outputs.update(declarations)
        available.update(outputs)


def audit(db, cid, name, old, new):
    db.execute(
        "INSERT INTO task_collection_parameter_overrides(collection_run_id,parameter_name,"
        "old_task_id,old_task_run_id,new_task_id,new_task_run_id,created_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,now())",
        (
            cid,
            name,
            old["source_task_id"],
            old["source_run_id"],
            new["source_task_id"],
            new["source_run_id"],
        ),
    )


def flush_audits(db):
    """Outbox: delivery may repeat after a crash; audit ID is the deduplication key."""
    rows = db.execute(
        "SELECT * FROM task_collection_parameter_overrides WHERE logged_at IS NULL "
        "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100",
    ).fetchall()
    for row in rows:
        LOGGER.error(
            "PARAMETER_OVERRIDDEN audit_id=%s collection_run_id=%s parameter_name=%s "
            "old_task_id=%s old_task_run_id=%s new_task_id=%s new_task_run_id=%s",
            row["id"],
            row["collection_run_id"],
            row["parameter_name"],
            row["old_task_id"],
            row["old_task_run_id"],
            row["new_task_id"],
            row["new_task_run_id"],
        )
        db.execute(
            "UPDATE task_collection_parameter_overrides SET logged_at=now() WHERE id=%s",
            (row["id"],),
        )


def finish_collection(db, cid, code, message):
    from app.service.task_execution import finish_failure

    pending = db.execute(
        "SELECT task_run_id,status FROM task_collection_run_members "
        "WHERE collection_run_id=%s AND status IN ('pending','running') FOR UPDATE",
        (cid,),
    ).fetchall()
    for row in pending:
        child_code = "COLLECTION_MEMBER_SKIPPED" if code == "COLLECTION_MEMBER_FAILED" else code
        finish_failure(db, row["task_run_id"], child_code, message)
    db.execute(
        "UPDATE task_collection_run_members SET status=CASE WHEN status='pending' "
        "THEN 'skipped' ELSE 'failed' END WHERE collection_run_id=%s "
        "AND status IN ('pending','running')",
        (cid,),
    )
    db.execute(
        "UPDATE task_collection_runs SET status='failed',error_code=%s,error_message=%s,"
        "finished_at=now() WHERE id=%s AND status IN ('queued','running')",
        (code, message, cid),
    )


def recover(db, before):
    rows = db.execute(
        "SELECT id FROM task_collection_runs WHERE status IN ('queued','running') "
        "AND created_at<%s FOR UPDATE",
        (before,),
    ).fetchall()
    for row in rows:
        # Preserve already terminal member states when leadership is recovered.
        db.execute(
            "UPDATE task_collection_run_members m SET status=r.status FROM task_runs r "
            "WHERE m.collection_run_id=%s AND m.task_run_id=r.id "
            "AND m.status='running' AND r.status IN ('succeeded','failed')",
            (row["id"],),
        )
        finish_collection(db, row["id"], "EXECUTION_INTERRUPTED", "服务重启，执行中断")


def advance(db):
    """Advance barriers without blocking the dispatcher or holding execution slots."""
    db.execute(
        "UPDATE task_collection_runs SET status='running',started_at=now() WHERE status='queued'",
    )
    collections = db.execute(
        "SELECT * FROM task_collection_runs WHERE status='running' ORDER BY id FOR UPDATE",
    ).fetchall()
    for collection in collections:
        cid = collection["id"]
        db.execute(
            "UPDATE task_collection_run_members m SET status=r.status FROM task_runs r "
            "WHERE m.collection_run_id=%s AND m.task_run_id=r.id "
            "AND m.status='running' AND r.status IN ('succeeded','failed')",
            (cid,),
        )
        rows = db.execute(
            "SELECT * FROM task_collection_run_members WHERE collection_run_id=%s "
            "ORDER BY position,task_id",
            (cid,),
        ).fetchall()
        context = collection["global_parameters"]
        # Freezing the next batch inputs and setting ready is the durable merge marker.
        # The final batch merge and parent success commit in the same transaction.
        batches = [list(b) for _, b in groupby(rows, key=lambda r: r["position"])]
        for index, batch in enumerate(batches):
            if any(r["status"] == "failed" for r in batch):
                from app.service.task_execution import finish_failure

                executing = False
                for member in batch:
                    if member["status"] not in {"pending", "running"}:
                        continue
                    child = db.execute(
                        "SELECT status FROM task_runs WHERE id=%s FOR UPDATE",
                        (member["task_run_id"],),
                    ).fetchone()
                    if child["status"] == "queued":
                        finish_failure(
                            db,
                            member["task_run_id"],
                            "COLLECTION_MEMBER_SKIPPED",
                            "同批成员失败，尚未开始的任务跳过",
                        )
                        db.execute(
                            "UPDATE task_collection_run_members SET status='skipped' "
                            "WHERE collection_run_id=%s AND task_id=%s",
                            (cid, member["task_id"]),
                        )
                    else:
                        executing = True
                if not executing:
                    finish_collection(
                        db, cid, "COLLECTION_MEMBER_FAILED", "成员任务失败，后续执行跳过"
                    )
                break
            if any(r["status"] == "running" for r in batch):
                break
            if all(r["status"] == "pending" for r in batch):
                if batch[0]["ready"]:
                    break
                for row in batch:
                    db.execute(
                        "UPDATE task_collection_run_members SET input_parameters=%s,ready=true "
                        "WHERE collection_run_id=%s AND task_id=%s",
                        (Jsonb(context), cid, row["task_id"]),
                    )
                break
            if all(r["status"] == "succeeded" for r in batch):
                # Already merged when the following batch was made ready.
                if index + 1 < len(batches) and batches[index + 1][0]["ready"]:
                    continue
                context = copy.deepcopy(context)
                for row in batch:
                    params = db.execute(
                        "SELECT * FROM task_run_parameters WHERE run_id=%s AND status='succeeded' "
                        "ORDER BY position,id",
                        (row["task_run_id"],),
                    ).fetchall()
                    for p in params:
                        new = {
                            "value": p["value"],
                            "value_type": p["value_type"],
                            "sensitive": p["sensitive"],
                            "source_task_id": row["task_id"],
                            "source_run_id": row["task_run_id"],
                        }
                        old = context.get(p["name"])
                        if old is not None:
                            new["sensitive"] |= old["sensitive"]
                            audit(db, cid, p["name"], old, new)
                        context[p["name"]] = new
                db.execute(
                    "UPDATE task_collection_runs SET global_parameters=%s WHERE id=%s",
                    (Jsonb(context), cid),
                )
                if index == len(batches) - 1:
                    db.execute(
                        "UPDATE task_collection_runs SET status='succeeded',finished_at=now() "
                        "WHERE id=%s",
                        (cid,),
                    )
                # Continue to the next batch within the same transaction, so no repeat merge.
