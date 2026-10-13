"""Implementation of docs/rest-project.yaml.

Each operationId has a same-named function in this module. Route registration
binds these functions directly; search for `def <operationId>` to find the logic.
"""

import yaml

from app.api.handlers import health as health_handler
from app.api.handlers import ready as ready_handler
from app.repository.projects import FILE, PROJECT, SUMMARY, VERSION, required
from app.service.openapi_import import redact


async def health():
    """GET /api/v1/health"""
    return await health_handler()


def ready():
    """GET /api/v1/ready"""
    return ready_handler()


def listProjects(repository, path, query, body, upload):
    """GET /api/v1/projects"""
    return repository.page(
        PROJECT + " WHERE strpos(lower(p.name),lower(%s))>0 ORDER BY p.updated_at DESC,p.id",
        (query.get("name", ""),),
        query,
    )


def createProject(repository, path, query, body, upload):
    """POST /api/v1/projects"""
    row = repository.one(
        "INSERT INTO projects(name,description) VALUES (%s,%s) RETURNING id",
        (body["name"], body.get("description", "")),
    )
    return repository.one(PROJECT + " WHERE p.id=%s", (row["id"],))


def getProject(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}"""
    pid = path["project_id"]
    repository.project(pid)
    result = repository.one(PROJECT + " WHERE p.id=%s", (pid,))
    result["yaml_files"] = repository.db.execute(
        FILE + " WHERE f.project_id=%s ORDER BY f.updated_at DESC,f.id", (pid,)
    ).fetchall()
    return result


def updateProject(repository, path, query, body, upload):
    """PATCH /api/v1/projects/{project_id}"""
    pid = path["project_id"]
    row = repository.project(pid, lock=True)
    repository.db.execute(
        "UPDATE projects SET name=%s,description=%s,updated_at=now() WHERE id=%s",
        (body.get("name", row["name"]), body.get("description", row["description"]), pid),
    )
    return repository.one(PROJECT + " WHERE p.id=%s", (pid,))


def previewProjectDeletion(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/deletion-preview"""
    pid = path["project_id"]
    return repository.preview(pid)


def deleteProject(repository, path, query, body, upload):
    """DELETE /api/v1/projects/{project_id}"""
    pid = path["project_id"]
    repository.project(pid, lock=True)
    ids = [
        r["id"]
        for r in repository.db.execute(
            "SELECT id FROM yaml_files WHERE project_id=%s FOR UPDATE", (pid,)
        )
    ]
    if repository.one("SELECT to_regclass('tasks') AS table_name")["table_name"]:
        from app.repository.rest_task import delete_task_rows

        tasks = repository.db.execute(
            "SELECT id FROM tasks WHERE project_id=%s ORDER BY id FOR UPDATE", (pid,)
        ).fetchall()
        for row in tasks:
            delete_task_rows(repository, row["id"])
    repository.delete_files(ids)
    repository.db.execute("DELETE FROM projects WHERE id=%s", (pid,))
    return {"id": pid, "deleted": True}


def listYamlFiles(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files"""
    pid = path["project_id"]
    repository.project(pid)
    return repository.page(
        FILE + " WHERE f.project_id=%s ORDER BY f.updated_at DESC,f.id", (pid,), query
    )


def importYamlFile(repository, path, query, body, upload):
    """POST /api/v1/projects/{project_id}/yaml-files"""
    pid = path["project_id"]
    return repository.save_upload(pid, None, body, upload)


def getYamlFile(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid)
    repository.file(pid, fid)
    return repository.one(FILE + " WHERE f.id=%s", (fid,))


def previewYamlFileDeletion(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/deletion-preview"""
    pid, fid = path["project_id"], path["file_id"]
    return repository.preview(pid, fid)


def deleteYamlFile(repository, path, query, body, upload):
    """DELETE /api/v1/projects/{project_id}/yaml-files/{file_id}"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid, lock=True)
    repository.file(pid, fid, lock=True)
    repository.delete_files([fid])
    return {"id": fid, "deleted": True}


def updateYamlFile(repository, path, query, body, upload):
    """POST /api/v1/projects/{project_id}/yaml-files/{file_id}/versions"""
    pid, fid = path["project_id"], path["file_id"]
    return repository.save_upload(pid, fid, body, upload)


def listYamlVersions(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/versions"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid)
    repository.file(pid, fid)
    return repository.page(VERSION + " WHERE v.file_id=%s ORDER BY v.version DESC", (fid,), query)


def listInterfaces(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/interfaces"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid)
    file = repository.file(pid, fid)
    version = query.get("version", file["current_version"])
    required(
        repository.one(
            "SELECT version FROM yaml_versions WHERE file_id=%s AND version=%s", (fid, version)
        )
    )
    return repository.page(
        "SELECT "
        + SUMMARY
        + """ FROM api_interfaces WHERE file_id=%s AND version=%s
                AND (%s='' OR method=%s) AND strpos(path,%s)>0 ORDER BY path,method""",
        (fid, version, query.get("method", ""), query.get("method", ""), query.get("path", "")),
        query,
    )


def getInterface(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/interfaces/{interface_id}"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid)
    repository.file(pid, fid)
    row = required(
        repository.one(
            "SELECT * FROM api_interfaces WHERE file_id=%s AND id=%s", (fid, path["interface_id"])
        )
    )
    if row["request_body"] is None:
        del row["request_body"]
    return row


def testInterface(repository, path, query, body, upload):
    """POST /api/v1/projects/{project_id}/yaml-files/{file_id}/interfaces/{interface_id}/test"""
    from app.service.interface_test import execute_once

    repository.project(path["project_id"])
    repository.file(path["project_id"], path["file_id"])
    interface = required(
        repository.one(
            "SELECT * FROM api_interfaces WHERE file_id=%s AND id=%s",
            (path["file_id"], path["interface_id"]),
        )
    )
    version = required(
        repository.one(
            "SELECT parsed_document FROM yaml_versions WHERE file_id=%s AND version=%s",
            (interface["file_id"], interface["version"]),
        )
    )
    # Retain the chosen immutable version in memory and release the read transaction.
    repository.db.commit()
    return execute_once(interface, version["parsed_document"], body, path["project_id"])


def getYamlVersion(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/versions/{version_number}"""
    pid, fid = path["project_id"], path["file_id"]
    repository.project(pid)
    repository.file(pid, fid)
    return required(
        repository.one(
            VERSION + " WHERE v.file_id=%s AND v.version=%s", (fid, path["version_number"])
        )
    )


def getYamlVersionContent(repository, path, query, body, upload):
    """GET /api/v1/projects/{project_id}/yaml-files/{file_id}/versions/{version_number}/content"""
    pid, fid, version = path["project_id"], path["file_id"], path["version_number"]
    repository.project(pid)
    repository.file(pid, fid)
    raw = required(
        repository.one(
            "SELECT parsed_document FROM yaml_versions WHERE file_id=%s AND version=%s",
            (fid, version),
        )
    )
    safe = redact(raw["parsed_document"])
    return {
        "file_id": fid,
        "version": version,
        "redacted": safe != raw["parsed_document"],
        "content": yaml.safe_dump(safe, allow_unicode=True, sort_keys=False),
    }


# The YAML operationId is the lookup key; each value points to a real function.
SYSTEM_OPERATIONS = {"health", "ready"}
OPERATIONS = {
    "health": health,
    "ready": ready,
    "listProjects": listProjects,
    "createProject": createProject,
    "getProject": getProject,
    "updateProject": updateProject,
    "deleteProject": deleteProject,
    "previewProjectDeletion": previewProjectDeletion,
    "listYamlFiles": listYamlFiles,
    "importYamlFile": importYamlFile,
    "getYamlFile": getYamlFile,
    "deleteYamlFile": deleteYamlFile,
    "previewYamlFileDeletion": previewYamlFileDeletion,
    "listYamlVersions": listYamlVersions,
    "updateYamlFile": updateYamlFile,
    "getYamlVersion": getYamlVersion,
    "getYamlVersionContent": getYamlVersionContent,
    "listInterfaces": listInterfaces,
    "getInterface": getInterface,
    "testInterface": testInterface,
}
