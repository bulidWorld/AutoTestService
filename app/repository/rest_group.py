"""Task-scoped ordered groups and runs for rest-group.yaml."""

from app.repository.projects import required
from app.repository.rest_task import active, configuration, create_run, task
from app.service.errors import BusinessError, invalid
from app.service.task_groups import group_configuration

SYSTEM_OPERATIONS = set()


def group(repository, path):
    return required(
        repository.one(
            "SELECT * FROM task_groups WHERE task_id=%s AND id=%s",
            (path["task_id"], path["group_id"]),
        )
    )


def members(repository, row):
    row["step_ids"] = [
        r["step_id"]
        for r in repository.db.execute(
            "SELECT step_id FROM task_group_steps WHERE group_id=%s ORDER BY position",
            (row["id"],),
        ).fetchall()
    ]
    return row


def idle(repository, tid):
    if active(repository, tid):
        raise BusinessError(409, "TASK_ACTIVE_RUN", "任务存在未结束运行")


def listTaskGroups(repository, path, query, body, upload):
    task(repository, path)
    page = repository.page(
        "SELECT * FROM task_groups WHERE task_id=%s AND strpos(lower(name),lower(%s))>0 "
        "ORDER BY updated_at DESC,id",
        (path["task_id"], query.get("name", "")),
        query,
    )
    page["items"] = [members(repository, row) for row in page["items"]]
    return page


def getTaskGroup(repository, path, query, body, upload):
    task(repository, path)
    return members(repository, group(repository, path))


def save_group(repository, path, body, creating=False):
    task(repository, path, lock=True)
    existing = None if creating else group(repository, path)
    idle(repository, path["task_id"])
    name = body["name"].strip()
    if not name:
        invalid("组名不得为空", "body.name")
    if repository.one(
        "SELECT id FROM task_groups WHERE task_id=%s AND name=%s "
        "AND (%s::integer IS NULL OR id<>%s)",
        (
            path["task_id"],
            name,
            existing["id"] if existing else None,
            existing["id"] if existing else None,
        ),
    ):
        raise BusinessError(409, "GROUP_NAME_CONFLICT", "任务内组名已存在", "body.name")
    ids = body["step_ids"]
    rows = repository.db.execute(
        "SELECT id FROM task_steps WHERE task_id=%s AND id=ANY(%s)",
        (path["task_id"], ids),
    ).fetchall()
    if len(set(ids)) != len(ids) or {r["id"] for r in rows} != set(ids):
        invalid("组成员重复或不属于当前任务", "body.step_ids")
    if creating:
        row = repository.one(
            "INSERT INTO task_groups(task_id,name,description) VALUES(%s,%s,%s) RETURNING *",
            (path["task_id"], name, body.get("description", "")),
        )
    else:
        row = repository.one(
            "UPDATE task_groups SET name=%s,description=%s,updated_at=now() WHERE id=%s RETURNING *",
            (name, body.get("description", ""), existing["id"]),
        )
        repository.db.execute("DELETE FROM task_group_steps WHERE group_id=%s", (row["id"],))
    for position, sid in enumerate(ids, 1):
        repository.db.execute(
            "INSERT INTO task_group_steps(group_id,step_id,position) VALUES(%s,%s,%s)",
            (row["id"], sid, position),
        )
    return members(repository, row)


def createTaskGroup(repository, path, query, body, upload):
    return save_group(repository, path, body, creating=True)


def updateTaskGroup(repository, path, query, body, upload):
    return save_group(repository, path, body)


def deleteTaskGroup(repository, path, query, body, upload):
    task(repository, path, lock=True)
    row = group(repository, path)
    idle(repository, path["task_id"])
    repository.db.execute("DELETE FROM task_group_steps WHERE group_id=%s", (row["id"],))
    repository.db.execute("DELETE FROM task_groups WHERE id=%s", (row["id"],))


def startTaskGroupRun(repository, path, query, body, upload):
    task(repository, path, lock=True)
    row = members(repository, group(repository, path))
    idle(repository, path["task_id"])
    config = group_configuration(configuration(repository, path), row)
    return create_run(repository, path, config, group_id=row["id"])


OPERATIONS = {
    f.__name__: f
    for f in [
        listTaskGroups,
        getTaskGroup,
        createTaskGroup,
        updateTaskGroup,
        deleteTaskGroup,
        startTaskGroupRun,
    ]
}
