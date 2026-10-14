"""Durable sequential task execution and a single PostgreSQL-elected dispatcher."""

import base64
import http.client
import json
import logging
import ssl
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.service.errors import BusinessError
from app.service.task_config import build_request, execution_plan, extract, scrub, sensitive_values

LOGGER = logging.getLogger(__name__)
LEADER_KEY = 741930512


def connection():
    # Reuse the application provider so isolated schema integration tests cover workers too.
    from app.repository import projects

    db = projects.connect()
    db.row_factory = dict_row
    return db


def request_http(request, timeout):
    """Exactly one request; http.client has neither redirects nor automatic retries."""
    url = urlsplit(request["actual_url"])
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    options = {"timeout": timeout}
    if url.scheme == "https":
        options["context"] = ssl.create_default_context()
    client = cls(url.hostname, url.port, **options)
    response = None
    started = time.monotonic()
    try:
        target = (url.path or "/") + ("?" + url.query if url.query else "")
        client.putrequest(request["method"], target, skip_accept_encoding=True)
        body = (
            request["request_body"].encode("utf-8") if request["request_body"] is not None else None
        )
        for h in request["request_headers"]:
            client.putheader(h["name"], h["value"])
        if body is not None:
            client.putheader("Content-Length", str(len(body)))
        client.endheaders(body)
        response = client.getresponse()
        status = response.status
        headers = [{"name": k, "value": v} for k, v in response.getheaders()]
        content = response.read()
        try:
            text = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        result = {
            "response_status": status,
            "response_headers": headers,
            "response_body": text,
            "response_body_encoding": encoding,
            "error_code": None,
            "error_message": None,
        }
        if not 200 <= status < 300:
            result.update(error_code="HTTP_STATUS_ERROR", error_message="HTTP 状态码不为 2xx")
        return result
    except TimeoutError:
        return {
            "response_status": response.status if response else None,
            "response_headers": [{"name": k, "value": v} for k, v in response.getheaders()]
            if response
            else None,
            "response_body": None,
            "response_body_encoding": None,
            "error_code": "TIMEOUT",
            "error_message": "请求超时",
        }
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        return {
            "response_status": response.status if response else None,
            "response_headers": None,
            "response_body": None,
            "response_body_encoding": None,
            "error_code": "CONNECTION_ERROR",
            "error_message": "连接或响应传输失败",
        }
    finally:
        client.close()
        # Duration includes connect, send and response read; independent of wall clock adjustments.
        request["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))


def response_json(response, location):
    if response["error_code"]:
        raise BusinessError(422, response["error_code"], response["error_message"], location)
    try:
        if response["response_body_encoding"] != "utf-8":
            raise ValueError
        return json.loads(
            response["response_body"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, TypeError):
        raise BusinessError(422, "RESPONSE_NOT_JSON", "初始化响应不是有效 JSON", location) from None


def preview(config, name):
    values, responses = {}, {}
    target = next((p for p in config["global_parameters"] if p["name"] == name), None)
    if not target or target["source_type"] != "rest":
        raise BusinessError(
            400, "INVALID_REQUEST", "目标必须为 REST 全局参数", "body.parameter_name"
        )
    for kind, p in execution_plan(config):
        if kind == "step":
            request = build_request(config, p, values=values)
            response = request_http(request, config.get("timeout_seconds", 30))
            responses[p["id"]] = response
            if response["error_code"]:
                raise BusinessError(422, response["error_code"], response["error_message"])
            continue
        i = config["global_parameters"].index(p)
        if p["source_type"] == "fixed":
            value = p["value"]
        else:
            source = p["rest_source"]
            sid = source["step_id"]
            location = f"body.global_parameters.{i}"
            value = extract(
                response_json(responses[sid], location),
                source["extract_expression"],
                p["value_type"],
                location,
            )
        values[p["name"]] = value
        if p["name"] == name:
            result = {
                "parameter_name": name,
                "value_type": p["value_type"],
                "redacted": p.get("sensitive", False),
            }
            if not result["redacted"]:
                result["value"] = value
            return scrub(result, sensitive_values(config, values))


def finish_failure(db, rid, code, message):
    db.execute(
        "UPDATE task_runs SET status='failed',finished_at=now(),duration_ms=(extract(epoch FROM (now()-coalesce(started_at,created_at)))*1000)::bigint,error_code=%s,error_message=%s WHERE id=%s AND status IN ('queued','running')",
        (code, message, rid),
    )
    for table in ["task_run_steps", "task_run_parameters"]:
        db.execute(
            f"UPDATE {table} SET status=CASE WHEN status='running' THEN 'failed' ELSE 'skipped' END,finished_at=now(),duration_ms=CASE WHEN started_at IS NOT NULL THEN (extract(epoch FROM (now()-started_at))*1000)::bigint ELSE NULL END,error_code=%s,error_message=%s WHERE run_id=%s AND status IN ('pending','running')",
            (code, message, rid),
        )


class RunExecutor:
    def __init__(self, rid, stop=None):
        self.rid = rid
        self.stop = stop or threading.Event()
        self.values = {}
        self.responses = {}

    def check_active(self, db):
        row = db.execute("SELECT status FROM task_runs WHERE id=%s", (self.rid,)).fetchone()
        if self.stop.is_set() or not row or row["status"] != "running":
            raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")

    def execute_step(self, config, step, phase):
        with connection() as db:
            self.check_active(db)
            result = db.execute(
                "SELECT * FROM task_run_steps WHERE run_id=%s AND step_id=%s FOR UPDATE",
                (self.rid, step["id"]),
            ).fetchone()
            if result["status"] != "pending":
                raise BusinessError(422, "EXECUTION_INTERRUPTED", "步骤已经执行，禁止重复请求")
            request = build_request(config, step, values=self.values)
            db.execute(
                "UPDATE task_run_steps SET status='running',started_at=now() WHERE id=%s",
                (result["id"],),
            )
            q = db.execute(
                "INSERT INTO task_requests(run_id,run_step_id,step_id,position,method,actual_url,request_headers,request_body,phase,redaction_metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    self.rid,
                    result["id"],
                    step["id"],
                    step["position"],
                    request["method"],
                    request["actual_url"],
                    Jsonb(request["request_headers"]),
                    request["request_body"],
                    phase,
                    Jsonb({"secrets": []}),
                ),
            ).fetchone()
        # Request record and running state are committed before any network bytes are sent.
        response = request_http(request, config["timeout_seconds"])
        with connection() as db:
            db.execute(
                "UPDATE task_requests SET response_status=%s,response_headers=%s,response_body=%s,response_body_encoding=%s,error_code=%s,error_message=%s,finished_at=now(),duration_ms=%s WHERE id=%s",
                (
                    response["response_status"],
                    Jsonb(response["response_headers"])
                    if response["response_headers"] is not None
                    else None,
                    response["response_body"],
                    response["response_body_encoding"],
                    response["error_code"],
                    response["error_message"],
                    request["duration_ms"],
                    q["id"],
                ),
            )
            db.execute(
                "UPDATE task_run_steps SET status=%s,finished_at=now(),duration_ms=%s,error_code=%s,error_message=%s WHERE id=%s",
                (
                    "failed" if response["error_code"] else "succeeded",
                    request["duration_ms"],
                    response["error_code"],
                    response["error_message"],
                    result["id"],
                ),
            )
        if response["error_code"]:
            raise BusinessError(422, response["error_code"], response["error_message"])
        return response, q["id"]

    def execute(self):
        config = None
        try:
            with connection() as db:
                row = db.execute("SELECT * FROM task_runs WHERE id=%s", (self.rid,)).fetchone()
                if not row or row["status"] != "running":
                    return
                config = row["config_snapshot"]
            for kind, p in execution_plan(config):
                if kind == "step":
                    self.responses[p["id"]] = self.execute_step(
                        config, p, p.get("execution_phase", "steps")
                    )
                    continue
                i = config["global_parameters"].index(p)
                with connection() as db:
                    self.check_active(db)
                    db.execute(
                        "UPDATE task_run_parameters SET status='running',started_at=now() WHERE run_id=%s AND parameter_id=%s",
                        (self.rid, p["id"]),
                    )
                qid = None
                sid = None
                try:
                    if p["source_type"] == "fixed":
                        value = p["value"]
                    else:
                        source = p["rest_source"]
                        sid = source["step_id"]
                        location = f"global_parameters.{i}"
                        response, qid = self.responses[sid]
                        with connection() as db:
                            db.execute(
                                "UPDATE task_run_parameters SET request_id=%s WHERE run_id=%s AND parameter_id=%s",
                                (qid, self.rid, p["id"]),
                            )
                        value = extract(
                            response_json(response, location),
                            source["extract_expression"],
                            p["value_type"],
                            location,
                        )
                    self.values[p["name"]] = value
                    with connection() as db:
                        db.execute(
                            "UPDATE task_run_parameters SET status='succeeded',value=%s,request_id=%s,finished_at=now(),duration_ms=(extract(epoch FROM (now()-started_at))*1000)::bigint WHERE run_id=%s AND parameter_id=%s",
                            (Jsonb(value), qid, self.rid, p["id"]),
                        )
                except BusinessError as exc:
                    with connection() as db:
                        if sid:
                            request_row = db.execute(
                                "SELECT id FROM task_requests WHERE run_id=%s AND step_id=%s ORDER BY id DESC LIMIT 1",
                                (self.rid, sid),
                            ).fetchone()
                            if request_row:
                                db.execute(
                                    "UPDATE task_run_parameters SET request_id=%s WHERE run_id=%s AND parameter_id=%s",
                                    (request_row["id"], self.rid, p["id"]),
                                )
                        db.execute(
                            "UPDATE task_run_parameters SET status='failed',finished_at=now(),duration_ms=(extract(epoch FROM (now()-started_at))*1000)::bigint,error_code=%s,error_message=%s WHERE run_id=%s AND parameter_id=%s",
                            (exc.payload["code"], exc.payload["detail"], self.rid, p["id"]),
                        )
                        if sid:
                            db.execute(
                                "UPDATE task_run_steps SET status='failed',finished_at=coalesce(finished_at,now()),error_code=%s,error_message=%s WHERE run_id=%s AND step_id=%s",
                                (exc.payload["code"], exc.payload["detail"], self.rid, sid),
                            )
                    raise
            with connection() as db:
                self.check_active(db)
                db.execute(
                    "UPDATE task_runs SET status='succeeded',finished_at=now(),duration_ms=(extract(epoch FROM (now()-started_at))*1000)::bigint WHERE id=%s",
                    (self.rid,),
                )
        except Exception as exc:  # noqa: BLE001 - worker must persist failures safely
            if isinstance(exc, BusinessError):
                code, message = exc.payload["code"], exc.payload["detail"]
            else:
                code, message = "EXECUTION_ERROR", "执行内部错误"
                LOGGER.error(
                    "任务执行内部错误 run_id=%s exception=%s", self.rid, type(exc).__name__
                )
            try:
                with connection() as db:
                    finish_failure(db, self.rid, code, message)
            except Exception:  # noqa: BLE001 - persistence errors must not expose credentials
                LOGGER.error("运行状态保存失败 run_id=%s", self.rid)


class Dispatcher:
    """Only the advisory-lock owner recovers interrupted work and dispatches queued runs."""

    def __init__(self):
        self.stop = threading.Event()
        self.created_at = datetime.now(UTC)
        self.workers = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="task-dispatcher", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.stop.is_set():
            try:
                with connection() as db:
                    db.autocommit = True
                    if not db.execute(
                        "SELECT CASE WHEN current_schema()='public' "
                        "THEN pg_try_advisory_lock(%s::bigint) "
                        "ELSE pg_try_advisory_lock(hashtext(current_schema()),%s::integer) END AS elected",
                        (LEADER_KEY, LEADER_KEY),
                    ).fetchone()["elected"]:
                        self.stop.wait(1)
                        continue
                    # Recover only once per leadership acquisition, before dispatching any new work.
                    with db.transaction():
                        unfinished = db.execute(
                            "SELECT id FROM task_runs WHERE status IN ('queued','running') AND created_at < %s FOR UPDATE",
                            (self.created_at,),
                        ).fetchall()
                        for row in unfinished:
                            finish_failure(
                                db, row["id"], "EXECUTION_INTERRUPTED", "服务重启，执行中断"
                            )
                            db.execute(
                                "UPDATE task_requests SET error_code='EXECUTION_INTERRUPTED',error_message='服务重启，执行中断',finished_at=now(),duration_ms=(extract(epoch FROM (now()-started_at))*1000)::bigint WHERE run_id=%s AND finished_at IS NULL",
                                (row["id"],),
                            )
                    while not self.stop.is_set():
                        self.workers = {rid: w for rid, w in self.workers.items() if w.is_alive()}
                        if len(self.workers) < 8:
                            with db.transaction():
                                row = db.execute(
                                    "SELECT id FROM task_runs WHERE status='queued' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"
                                ).fetchone()
                                if row:
                                    db.execute(
                                        "UPDATE task_runs SET status='running',started_at=now() WHERE id=%s",
                                        (row["id"],),
                                    )
                            if row:
                                rid = row["id"]
                                w = threading.Thread(
                                    target=RunExecutor(rid, self.stop).execute,
                                    name=f"task-run-{rid}",
                                    daemon=True,
                                )
                                self.workers[rid] = w
                                w.start()
                        self.stop.wait(0.1)
                    for worker in self.workers.values():
                        while worker.is_alive():
                            worker.join(timeout=0.1)
            except Exception as exc:  # noqa: BLE001 - worker must persist failures safely
                LOGGER.error("任务调度不可用 exception=%s", type(exc).__name__)
                # Never reacquire leadership while our previous workers can still send requests.
                if any(w.is_alive() for w in self.workers.values()):
                    self.stop.set()
                    return
                self.stop.wait(1)
