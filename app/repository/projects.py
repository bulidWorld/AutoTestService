"""Program-managed relationships. All mutations lock project before file."""

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.repository.database import connect
from app.service.errors import BusinessError
from app.service.openapi_import import redact

PROJECT = """SELECT p.*, (SELECT count(*) FROM yaml_files f WHERE f.project_id=p.id)
 AS yaml_file_count, (SELECT coalesce(sum(v.interface_count),0) FROM yaml_files f
 JOIN yaml_versions v ON v.file_id=f.id AND v.version=f.current_version
 WHERE f.project_id=p.id) AS interface_count FROM projects p"""
FILE = """SELECT f.*, v.interface_count FROM yaml_files f JOIN yaml_versions v
 ON v.file_id=f.id AND v.version=f.current_version"""
VERSION = """SELECT v.file_id,v.version,v.filename,v.imported_at,v.interface_count,
 v.openapi_version,v.title,v.api_version,(v.version=f.current_version) AS is_current
 FROM yaml_versions v JOIN yaml_files f ON f.id=v.file_id"""
SUMMARY = "id,file_id,version,method,path,operation_id,summary,description"


def required(row):
    if row is None:
        raise BusinessError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    return row


class Projects:
    def __init__(self, connection):
        self.db = connection
        self.db.row_factory = dict_row

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def project(self, pid, lock=False):
        return required(
            self.one("SELECT * FROM projects WHERE id=%s" + (" FOR UPDATE" if lock else ""), (pid,))
        )

    def file(self, pid, fid, lock=False):
        return required(
            self.one(
                "SELECT * FROM yaml_files WHERE project_id=%s AND id=%s"
                + (" FOR UPDATE" if lock else ""),
                (pid, fid),
            )
        )

    def page(self, sql, args, query):
        page, size = query.get("page", 1), query.get("page_size", 20)
        # sql contains trusted, fixed SQL only; values remain bound parameters.
        total = self.one("SELECT count(*) AS total FROM (" + sql + ") records", args)["total"]
        rows = self.db.execute(
            sql + " LIMIT %s OFFSET %s", (*args, size, (page - 1) * size)
        ).fetchall()
        return {"items": rows, "total": total, "page": page, "page_size": size}

    def preview(self, pid, fid=None):
        self.project(pid)
        if fid is not None:
            self.file(pid, fid)
        files = self.db.execute(
            "SELECT id FROM yaml_files WHERE project_id=%s" + (" AND id=%s" if fid else ""),
            (pid, fid) if fid else (pid,),
        ).fetchall()
        ids = [f["id"] for f in files]
        result = {
            "target_id": fid or pid,
            "yaml_file_count": len(ids),
            "version_count": self.one(
                "SELECT count(*) AS n FROM yaml_versions WHERE file_id=ANY(%s)", (ids,)
            )["n"],
            "interface_count": self.one(
                "SELECT count(*) AS n FROM api_interfaces WHERE file_id=ANY(%s)", (ids,)
            )["n"],
            "task_count": 0,
            "run_count": 0,
            "can_delete": True,
            "blocking_reasons": [],
        }

        if self.one("SELECT to_regclass('tasks') AS table_name")["table_name"]:
            tasks = self.db.execute(
                "SELECT DISTINCT t.id FROM tasks t "
                + (
                    "JOIN task_steps s ON s.task_id=t.id WHERE s.file_id=%s"
                    if fid
                    else "WHERE t.project_id=%s"
                ),
                (fid or pid,),
            ).fetchall()
            task_ids = [t["id"] for t in tasks]
            result["task_count"] = len(task_ids)
            result["run_count"] = self.one(
                "SELECT count(*) AS n FROM task_runs WHERE task_id=ANY(%s)", (task_ids,)
            )["n"]
            blocked = (
                bool(task_ids)
                if fid
                else self.one(
                    "SELECT count(*) AS n FROM task_runs WHERE task_id=ANY(%s) AND status IN ('queued','running')",
                    (task_ids,),
                )["n"]
                > 0
            )
            result["can_delete"] = not blocked
            result["blocking_reasons"] = (
                (["文件被任务步骤引用"] if fid else ["存在未结束运行"]) if blocked else []
            )
        return result

    def delete_files(self, ids):
        if self.one("SELECT to_regclass('task_steps') AS table_name")["table_name"] and self.one(
            "SELECT id FROM task_steps WHERE file_id=ANY(%s) LIMIT 1", (ids,)
        ):
            raise BusinessError(409, "FILE_REFERENCED", "文件被任务步骤引用，不能删除")
        for table in ("api_interfaces", "yaml_versions", "yaml_files"):
            column = "id" if table == "yaml_files" else "file_id"
            self.db.execute(f"DELETE FROM {table} WHERE {column}=ANY(%s)", (ids,))

    def save_upload(self, pid, fid, body, upload):
        self.project(pid, lock=True)
        if fid:
            file = self.file(pid, fid, lock=True)
            version = file["current_version"] + 1
        else:
            file = self.one(
                "INSERT INTO yaml_files(project_id,name) VALUES (%s,%s) RETURNING *",
                (pid, body["name"]),
            )
            fid, version = file["id"], 1
        document = upload["document"]
        self.db.execute(
            """INSERT INTO yaml_versions
            (file_id,version,filename,interface_count,openapi_version,title,api_version,
             original_content,parsed_document) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                fid,
                version,
                upload["filename"],
                len(upload["interfaces"]),
                document["openapi"],
                document["info"]["title"],
                document["info"]["version"],
                upload["original"],
                Jsonb(document),
            ),
        )
        for item in upload["interfaces"]:
            self.db.execute(
                """INSERT INTO api_interfaces
                (file_id,version,method,path,operation_id,summary,description,parameters,
                 request_body,responses,authentication,servers)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    fid,
                    version,
                    item["method"],
                    item["path"],
                    item["operation_id"],
                    item["summary"],
                    item["description"],
                    Jsonb(item["parameters"]),
                    Jsonb(item["request_body"]) if "request_body" in item else None,
                    Jsonb(item["responses"]),
                    Jsonb(item["authentication"]),
                    Jsonb(item["servers"]),
                ),
            )
        self.db.execute(
            "UPDATE yaml_files SET current_version=%s,updated_at=now() WHERE id=%s", (version, fid)
        )
        return {
            "file": self.one(FILE + " WHERE f.id=%s", (fid,)),
            "version": self.one(VERSION + " WHERE v.file_id=%s AND v.version=%s", (fid, version)),
        }


def execute(handler, path, query, body, upload=None):
    """Execute a directly bound operation in one transaction."""
    from psycopg.errors import UniqueViolation

    try:
        with connect() as connection:
            result = handler(Projects(connection), path, query, body, upload)
            # Task handlers perform typed, source-aware redaction. Generic YAML redaction
            # would corrupt typed object values whose property names resemble credentials.
            return (
                result
                if handler.__module__ in {"app.repository.rest_task", "app.repository.rest_group"}
                or handler.__name__ == "testInterface"
                else redact(result)
            )
    except UniqueViolation as exc:
        project = exc.diag.constraint_name == "uq_projects_name"
        raise BusinessError(
            409,
            "PROJECT_NAME_CONFLICT" if project else "FILE_NAME_CONFLICT",
            "项目名称已存在" if project else "同项目逻辑文件名已存在",
            "body.name",
        ) from None
