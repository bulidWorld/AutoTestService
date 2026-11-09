"""Project-scoped task collection operations, bound by the OpenAPI contract."""

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.repository.projects import required
from app.repository.rest_task import active, configuration, create_run
from app.service.errors import BusinessError, invalid
from app.service.task_collections import apply_overrides, validate_members, validate_overrides
from app.service.task_config import scrub, secret_strings

SYSTEM_OPERATIONS = set()
SUMMARY = (
    "id,collection_id,project_id,status,created_at,started_at,finished_at,error_code,error_message"
)


def collection(repository, path, lock=False):
    repository.project(path["project_id"], lock=lock)
    return required(
        repository.one(
            "SELECT * FROM task_collections WHERE project_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (path["project_id"], path["collection_id"]),
        )
    )


def members(repository, row):
    row["members"] = repository.db.execute(
        "SELECT task_id,position FROM task_collection_members WHERE collection_id=%s "
        "ORDER BY position,task_id",
        (row["id"],),
    ).fetchall()
    return row


def public_collection(row):
    row = dict(row)
    if row.get("datasource") and "password" in row["datasource"]:
        row["datasource"] = dict(row["datasource"], password="***")
    return row


def idle(repository, cid):
    if repository.one(
        "SELECT id FROM task_collection_runs WHERE collection_id=%s "
        "AND status IN ('queued','running') LIMIT 1",
        (cid,),
    ):
        raise BusinessError(409, "COLLECTION_ACTIVE_RUN", "任务组存在未结束运行")


def listTaskCollections(repository, path, query, body, upload):
    repository.project(path["project_id"])
    result = repository.page(
        "SELECT * FROM task_collections WHERE project_id=%s ORDER BY updated_at DESC,id",
        (path["project_id"],),
        query,
    )
    result["items"] = [public_collection(members(repository, r)) for r in result["items"]]
    return result


def getTaskCollection(repository, path, query, body, upload):
    return public_collection(members(repository, collection(repository, path)))


def save(repository, path, body, creating=False):
    repository.project(path["project_id"], lock=True)
    old = None if creating else collection(repository, path, lock=True)
    if old:
        idle(repository, old["id"])
    name = body["name"].strip()
    if not name:
        invalid("任务组名称不得为空", "body.name")
    base_url = body.get("base_url")
    datasource = body.get("datasource")
    validate_overrides(base_url, datasource)
    if repository.one(
        "SELECT id FROM task_collections WHERE project_id=%s AND name=%s "
        "AND (%s::integer IS NULL OR id<>%s)",
        (path["project_id"], name, old["id"] if old else None, old["id"] if old else None),
    ):
        raise BusinessError(409, "COLLECTION_NAME_CONFLICT", "项目内任务组名称已存在")
    ids = [m["task_id"] for m in body["members"]]
    rows = repository.db.execute(
        "SELECT id FROM tasks WHERE project_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE",
        (path["project_id"], ids),
    ).fetchall()
    if len(set(ids)) != len(ids) or {r["id"] for r in rows} != set(ids):
        invalid("成员重复或不属于当前项目", "body.members")
    if creating:
        row = repository.one(
            "INSERT INTO task_collections(project_id,name,description,base_url,datasource,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,now(),now()) RETURNING *",
            (path["project_id"], name, body.get("description", ""), base_url, Jsonb(datasource) if datasource else None),
        )
    else:
        row = repository.one(
            "UPDATE task_collections SET name=%s,description=%s,base_url=%s,datasource=%s,updated_at=now() "
            "WHERE id=%s RETURNING *",
            (name, body.get("description", ""), base_url, Jsonb(datasource) if datasource else None, old["id"]),
        )
        repository.db.execute(
            "DELETE FROM task_collection_members WHERE collection_id=%s",
            (row["id"],),
        )
    for member in body["members"]:
        repository.db.execute(
            "INSERT INTO task_collection_members(collection_id,task_id,position) VALUES(%s,%s,%s)",
            (row["id"], member["task_id"], member["position"]),
        )
    return public_collection(members(repository, row))


def createTaskCollection(repository, path, query, body, upload):
    return save(repository, path, body, creating=True)


def updateTaskCollection(repository, path, query, body, upload):
    return save(repository, path, body)


def deleteTaskCollection(repository, path, query, body, upload):
    row = collection(repository, path, lock=True)
    idle(repository, row["id"])
    repository.db.execute(
        "DELETE FROM task_collection_members WHERE collection_id=%s", (row["id"],)
    )
    repository.db.execute("DELETE FROM task_collections WHERE id=%s", (row["id"],))


def startTaskCollectionRun(repository, path, query, body, upload):
    row = members(repository, collection(repository, path, lock=True))
    idle(repository, row["id"])
    if not row["members"]:
        invalid("空任务组不能运行", "members")
    ids = [m["task_id"] for m in row["members"]]
    locked = repository.db.execute(
        "SELECT id FROM tasks WHERE project_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE",
        (path["project_id"], ids),
    ).fetchall()
    if {r["id"] for r in locked} != set(ids):
        invalid("成员任务不存在或归属错误", "members")
    configs = []
    for member in row["members"]:
        if active(repository, member["task_id"]):
            raise BusinessError(409, "TASK_ACTIVE_RUN", "成员任务已被运行预约")
        configs.append(
            {
                "position": member["position"],
                "config": apply_overrides(
                    configuration(repository, dict(path, task_id=member["task_id"])), row
                ),
            }
        )
    validate_members(configs)
    snapshot = jsonable_encoder({"collection": row, "members": configs})
    run = repository.one(
        "INSERT INTO task_collection_runs(collection_id,project_id,status,config_snapshot,"
        "global_parameters,created_at) VALUES(%s,%s,'queued',%s,'{}'::jsonb,now()) "
        "RETURNING " + SUMMARY,
        (row["id"], path["project_id"], Jsonb(snapshot)),
    )
    for member in snapshot["members"]:
        config = member["config"]
        config["collection_run_id"] = run["id"]
        child = create_run(repository, dict(path, task_id=config["id"]), config)
        repository.db.execute(
            "INSERT INTO task_collection_run_members(collection_run_id,task_id,position,"
            "task_run_id,status,input_parameters) VALUES(%s,%s,%s,%s,'pending','{}'::jsonb)",
            (run["id"], config["id"], member["position"], child["id"]),
        )
    return run


def history(repository, path):
    repository.project(path["project_id"])
    if not repository.one(
        "SELECT id FROM task_collections WHERE id=%s AND project_id=%s UNION ALL "
        "SELECT collection_id AS id FROM task_collection_runs WHERE collection_id=%s "
        "AND project_id=%s LIMIT 1",
        (path["collection_id"], path["project_id"], path["collection_id"], path["project_id"]),
    ):
        required(None)


def run(repository, path):
    repository.project(path["project_id"])
    return required(
        repository.one(
            "SELECT * FROM task_collection_runs WHERE id=%s AND collection_id=%s AND project_id=%s",
            (path["run_id"], path["collection_id"], path["project_id"]),
        )
    )


def listTaskCollectionRuns(repository, path, query, body, upload):
    history(repository, path)
    return repository.page(
        "SELECT " + SUMMARY + " FROM task_collection_runs WHERE project_id=%s "
        "AND collection_id=%s ORDER BY created_at DESC,id",
        (path["project_id"], path["collection_id"]),
        query,
    )


def getTaskCollectionRun(repository, path, query, body, upload):
    row = run(repository, path)
    row.pop("config_snapshot")
    values = row.pop("global_parameters")
    secrets = [s for p in values.values() if p["sensitive"] for s in secret_strings(p["value"])]
    row["global_parameters"] = [
        dict(
            name=name,
            **dict(value, value="***" if value["sensitive"] else scrub(value["value"], secrets)),
        )
        for name, value in sorted(values.items())
    ]
    row["members"] = repository.db.execute(
        "SELECT task_id,position,task_run_id,status FROM task_collection_run_members "
        "WHERE collection_run_id=%s ORDER BY position,task_id",
        (row["id"],),
    ).fetchall()
    return row


def listTaskCollectionOverrides(repository, path, query, body, upload):
    row = run(repository, path)
    return repository.page(
        "SELECT id,parameter_name,old_task_id,old_task_run_id,new_task_id,new_task_run_id,created_at "
        "FROM task_collection_parameter_overrides WHERE collection_run_id=%s ORDER BY id",
        (row["id"],),
        query,
    )


def delete_project_rows(repository, pid):
    if repository.one(
        "SELECT id FROM task_collection_runs WHERE project_id=%s "
        "AND status IN ('queued','running') LIMIT 1",
        (pid,),
    ):
        raise BusinessError(409, "COLLECTION_ACTIVE_RUN", "项目存在未结束任务组运行")
    # A task may have been deleted after its collection: keep those child histories
    # until the project itself is removed, then clean them even without a tasks row.
    child_ids = [
        row["task_run_id"]
        for row in repository.db.execute(
            "SELECT m.task_run_id FROM task_collection_run_members m JOIN task_collection_runs r "
            "ON r.id=m.collection_run_id WHERE r.project_id=%s",
            (pid,),
        ).fetchall()
    ]
    for table in ["task_requests", "task_run_steps", "task_run_parameters"]:
        repository.db.execute(f"DELETE FROM {table} WHERE run_id=ANY(%s)", (child_ids,))
    repository.db.execute("DELETE FROM task_runs WHERE id=ANY(%s)", (child_ids,))
    for table in ["task_collection_parameter_overrides", "task_collection_run_members"]:
        repository.db.execute(
            f"DELETE FROM {table} WHERE collection_run_id IN "
            "(SELECT id FROM task_collection_runs WHERE project_id=%s)",
            (pid,),
        )
    repository.db.execute("DELETE FROM task_collection_runs WHERE project_id=%s", (pid,))
    repository.db.execute(
        "DELETE FROM task_collection_members WHERE collection_id IN "
        "(SELECT id FROM task_collections WHERE project_id=%s)",
        (pid,),
    )
    repository.db.execute("DELETE FROM task_collections WHERE project_id=%s", (pid,))


OPERATIONS = {
    f.__name__: f
    for f in [
        listTaskCollections,
        getTaskCollection,
        createTaskCollection,
        updateTaskCollection,
        deleteTaskCollection,
        startTaskCollectionRun,
        listTaskCollectionRuns,
        getTaskCollectionRun,
        listTaskCollectionOverrides,
    ]
}
