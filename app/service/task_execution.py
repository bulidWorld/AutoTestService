"""Durable sequential task execution and a single PostgreSQL-elected dispatcher."""

import base64
import copy
import http.client
import json
import logging
import ssl
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config.logging import step_context
from app.service.assertions import assertion_parameters, evaluate_assertion
from app.service.errors import BusinessError
from app.service.task_expressions import compute_values
from app.service.task_config import TYPES, build_request, extract, scrub, sensitive_values
from app.service.task_nodes import parameter_sources, siblings
from app.service.task_semantics import condition_action, wait_milliseconds

LOGGER = logging.getLogger(__name__)
LEADER_KEY = 741930512
HTTP_SLOTS = threading.BoundedSemaphore(8)
CHILD_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="task-child")


def connection():
    # Reuse the application provider so isolated schema integration tests cover workers too.
    from app.repository import projects

    db = projects.connect()
    db.row_factory = dict_row
    return db


def request_http(request, timeout):
    """Exactly one request; http.client has neither redirects nor automatic retries."""
    started = time.monotonic()
    completed_ms = None
    url = urlsplit(request["actual_url"])
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    options = {"timeout": timeout}
    if url.scheme == "https":
        options["context"] = ssl.create_default_context()
    client = cls(url.hostname, url.port, **options)
    response = None
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
        completed_ms = max(0, round((time.monotonic() - started) * 1000, 3))
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
        request["duration_ms"] = (
            completed_ms
            if completed_ms is not None
            else max(0, round((time.monotonic() - started) * 1000, 3))
        )


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


def apply_assertion(step, response, values):
    """Replace the HTTP verdict only after a complete response, before extraction."""
    code = step.get("assertion_code")
    if not code or not code.strip() or response["error_code"] not in (None, "HTTP_STATUS_ERROR"):
        return response
    judged = dict(response, error_code=None, error_message=None)
    try:
        body = response_json(judged, "assertion_code")
    except BusinessError:
        body = None
    headers = {}
    for header in response.get("response_headers") or []:
        headers.setdefault(header["name"].lower(), []).append(header["value"])
    diagnostics = {}
    error = evaluate_assertion(code, body, values, response.get("response_status"), headers,
                               diagnostics=diagnostics)
    messages = {
        "ASSERTION_FAILED": "业务断言未通过",
        "ASSERTION_EXECUTION_ERROR": "业务断言执行异常或超过资源限制",
        "ASSERTION_RETURN_TYPE_ERROR": "业务断言必须返回 boolean",
        "PRECONDITION_BLOCKED": "用例前提不足，执行阻塞",
    }
    message = messages.get(error)
    if diagnostics:
        summary = diagnostics["reason"]
        if diagnostics.get("exception"):
            summary += ": " + diagnostics["exception"]
        for frame in diagnostics.get("stack", []):
            summary += f"\n  at {frame['function']} (<assertion>:{frame['line']})"
        message += "：" + summary
        LOGGER.error("业务断言执行失败", extra={"stage": "assertion", **diagnostics})
    elif error:
        LOGGER.error(messages.get(error), extra={"stage": "assertion"})
    judged.update(error_code=error, error_message=message)
    return judged


def wait_until(milliseconds, stop):
    deadline = time.monotonic() + milliseconds / 1000
    while True:
        remaining = deadline - time.monotonic()
        if stop.is_set():
            raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
        if remaining <= 0:
            return
        stop.wait(min(remaining, 60))


def extract_values(config, step, response):
    parameters = parameter_sources(config).get(step["id"], [])
    if not parameters:
        return {}
    # Extraction is independent of the HTTP/assertion verdict. Incomplete responses
    # have no usable document; every attempted source still assigns a null fallback.
    usable = dict(response)
    if usable["error_code"] == "HTTP_STATUS_ERROR":
        usable.update(error_code=None, error_message=None)
    try:
        document = response_json(usable, "global_parameters")
    except BusinessError:
        document = None
    writes = {}
    for p in parameters:
        try:
            writes[p["name"]] = extract(
                document,
                p["rest_source"]["extract_expression"],
                p["value_type"],
                "global_parameters." + p["name"],
            )
        except BusinessError as exc:
            writes[p["name"]] = None
            LOGGER.warning("参数提取失败，已赋 null", extra={"stage": "extraction", "code": exc.payload["code"], "detail": p["name"]})
    return writes


def check_inputs(config, step, values):
    declarations = {p["name"]: p for p in config["global_parameters"]}
    if assertion_parameters(step.get("assertion_code"), declarations) - values.keys():
        raise BusinessError(422, "PARAMETER_NOT_AVAILABLE", "断言依赖的参数尚未发布")
    return build_request(config, step, values=values)


def preview(config, name):
    target = next((p for p in config["global_parameters"] if p["name"] == name), None)
    if not target or target["source_type"] not in {"rest", "expression"}:
        raise BusinessError(
            400, "INVALID_REQUEST", "目标必须为 REST 或表达式参数", "body.parameter_name"
        )
    parameter_sources(config)
    target_id = target[
        "expression_source" if target["source_type"] == "expression" else "rest_source"
    ]["step_id"]
    target_scope = next(s["execution_scope"] for s in config["steps"] if s["id"] == target_id)
    values = {
        p["name"]: copy.deepcopy(p["value"])
        for p in config["global_parameters"]
        if p["source_type"] == "fixed"
    }

    def leaf(step, inputs, abort=None):
        token = step_context.set({"runId": str(uuid4()), "task_step_id": step["id"], "operation": "previewRestParameter"})
        try:
            LOGGER.info("预览步骤开始", extra={"stage": "step"})
            return preview_leaf(step, inputs, abort)
        finally:
            LOGGER.info("预览步骤结束", extra={"stage": "step"})
            step_context.reset(token)

    def preview_leaf(step, inputs, abort=None):
        action = condition_action(step, inputs)
        if action == "skip":
            return {}, None
        if action == "blocked":
            raise BusinessError(422, "PRECONDITION_BLOCKED", "节点前提不足，执行阻塞")
        if abort and abort.is_set():
            return {}, None
        if step["step_type"] == "compute":
            try:
                return compute_values(config, step, inputs), None
            except Exception:
                if abort:
                    abort.set()
                raise
        if step["step_type"] == "wait":
            wait_until(wait_milliseconds(step, inputs), threading.Event())
            return {}, None
        try:
            with HTTP_SLOTS:
                if abort and abort.is_set():
                    return {}, None
                request = check_inputs(config, step, inputs)
                response = request_http(request, config.get("timeout_seconds", 30))
                writes = extract_values(config, step, response)
                response = apply_assertion(step, response, inputs)
                if response["error_code"]:
                    if abort:
                        abort.set()
                    return writes, BusinessError(
                        422, response["error_code"], response["error_message"]
                    )
                return writes, None
        except Exception:
            if abort:
                abort.set()
            raise

    steps = siblings(config)
    if target_scope == "finally":
        steps += siblings(config, "finally")
    main_failed = False
    for step in steps:
        if main_failed and step["execution_scope"] == "main":
            continue
        error = None
        reached = step["id"] == target_id
        if step["step_type"] == "parallel":
            action = condition_action(step, values)
            if action == "skip":
                if any(c["id"] == target_id for c in siblings(config, parent=step["id"])):
                    raise BusinessError(422, "PARAMETER_NOT_AVAILABLE", "目标来源被条件跳过")
                continue
            if action == "blocked":
                if target_scope != "finally":
                    raise BusinessError(422, "PRECONDITION_BLOCKED", "并发节点前提不足")
                main_failed = True
                continue
            children = siblings(config, parent=step["id"])
            reached = any(c["id"] == target_id for c in children)
            # Only submit up to this node's concurrency, stopping queued work after failure.
            outputs, errors = {}, []
            pending, iterator = {}, iter(children)
            abort = threading.Event()

            def submit(iterator=iterator, pending=pending, abort=abort):
                child = None if abort.is_set() else next(iterator, None)
                if child is not None:
                    pending[CHILD_POOL.submit(leaf, child, copy.deepcopy(values), abort)] = child

            for _ in range(step["max_concurrency"]):
                submit()
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    child = pending.pop(future)
                    try:
                        writes, error = future.result()
                        outputs.update(writes)
                        if error:
                            errors.append((child["position"], error))
                    except Exception as exc:  # noqa: BLE001 - drain all started previews before returning
                        errors.append((child["position"], exc))
                if not errors:
                    while len(pending) < step["max_concurrency"]:
                        before = len(pending)
                        submit()
                        if len(pending) == before:
                            break
            values.update(outputs)
            target_assigned = name in outputs
            if errors and not (reached and name in outputs):
                error = min(errors, key=lambda e: e[0])[1]
        else:
            try:
                writes, error = leaf(step, values)
            except BusinessError as exc:
                writes, error = {}, exc
            values.update(writes)
            target_assigned = name in writes
        if error and not (reached and target_assigned):
            if target_scope != "finally" or reached:
                raise error
            main_failed |= step["execution_scope"] == "main"
        if reached:
            if not target_assigned:
                raise BusinessError(422, "PARAMETER_NOT_AVAILABLE", "目标来源被条件跳过")
            result = {
                "parameter_name": name,
                "value_type": target["value_type"],
                "redacted": target.get("sensitive", False),
            }
            if not result["redacted"]:
                result["value"] = values[name]
            return scrub(result, sensitive_values(config, values))
    raise BusinessError(400, "INVALID_REQUEST", "目标来源不在执行流程", "body.parameter_name")


def finish_failure(db, rid, code, message):
    """Interrupt unrecoverable work; never replay uncertain sends or synthesize responses."""
    db.execute(
        "UPDATE task_runs SET status='failed',execution_stage='finished',"
        "cleanup_status=CASE WHEN execution_stage='finally' THEN 'failed' ELSE 'skipped' END,"
        "primary_error_code=CASE WHEN execution_stage='main' THEN coalesce(primary_error_code,%s) ELSE primary_error_code END,"
        "primary_error_message=CASE WHEN execution_stage='main' THEN coalesce(primary_error_message,%s) ELSE primary_error_message END,"
        "finished_at=now(),duration_ms=(extract(epoch FROM (now()-coalesce(started_at,created_at)))*1000)::bigint,"
        "error_code=%s,error_message=%s "
        "WHERE id=%s AND status IN ('queued','running')",
        (code, message, code, message, rid),
    )
    for table in ["task_run_steps", "task_run_parameters"]:
        child_duration = (
            "WHEN parent_run_step_id IS NOT NULL THEN NULL " if table == "task_run_steps" else ""
        )
        db.execute(
            f"UPDATE {table} SET status=CASE WHEN status='running' THEN 'failed' ELSE 'skipped' END,"
            f"finished_at=now(),duration_ms=CASE {child_duration}WHEN started_at IS NOT NULL THEN "
            "(extract(epoch FROM (now()-started_at))*1000)::bigint ELSE NULL END,error_code=%s,"
            "error_message=%s WHERE run_id=%s AND status IN ('pending','running')",
            (code, message, rid),
        )
    parents = db.execute(
        "SELECT id,parallel_stats FROM task_run_steps WHERE run_id=%s AND step_type='parallel' FOR UPDATE",
        (rid,),
    ).fetchall()
    for parent in parents:
        stats = parent["parallel_stats"]
        if stats is None:
            continue
        for status in ("pending", "running", "succeeded", "failed", "blocked", "skipped"):
            stats[status + "_count"] = 0
        for row in db.execute(
            "SELECT status,count(*) AS n FROM task_run_steps WHERE parent_run_step_id=%s AND step_type='rest' GROUP BY status",
            (parent["id"],),
        ).fetchall():
            stats[row["status"] + "_count"] = row["n"]
        db.execute(
            "UPDATE task_run_steps SET parallel_stats=%s WHERE id=%s", (Jsonb(stats), parent["id"])
        )


class RunExecutor:
    def __init__(self, rid, stop=None):
        self.rid = rid
        self.stop = stop or threading.Event()
        self.values = {}

    def check_active(self, db):
        row = db.execute("SELECT status FROM task_runs WHERE id=%s", (self.rid,)).fetchone()
        if self.stop.is_set() or not row or row["status"] != "running":
            raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")

    def stats_transition(self, db, parent, before, after, response=None, duration=None, sent=False):
        if parent is None:
            return
        row = db.execute(
            "SELECT parallel_stats FROM task_run_steps WHERE run_id=%s AND step_id=%s FOR UPDATE",
            (self.rid, parent),
        ).fetchone()
        stats = row["parallel_stats"]
        stats[before + "_count"] -= 1
        stats[after + "_count"] += 1
        if sent:
            stats["request_count"] += 1
        if response is not None and response.get("response_body") is not None:
            stats["response_count"] += 1
            stats["response_time_sum_ms"] = round(stats["response_time_sum_ms"] + duration, 3)
            stats["avg_response_time_ms"] = round(
                stats["response_time_sum_ms"] / stats["response_count"], 3
            )
            stats["max_response_time_ms"] = max(stats["max_response_time_ms"] or 0, duration)
            stats["min_response_time_ms"] = (
                duration
                if stats["min_response_time_ms"] is None
                else min(stats["min_response_time_ms"], duration)
            )
        db.execute(
            "UPDATE task_run_steps SET parallel_stats=%s WHERE run_id=%s AND step_id=%s",
            (Jsonb(stats), self.rid, parent),
        )

    def skip_node(self, step, code, status="skipped"):
        message = (
            "前提不足，执行阻塞"
            if status == "blocked"
            else ("节点条件未满足，未执行" if code == "CONDITION_NOT_MET" else "未启动的节点已跳过")
        )
        with connection() as db:
            row = db.execute(
                "UPDATE task_run_steps SET status=%s,finished_at=now(),error_code=%s,error_message=%s WHERE run_id=%s AND step_id=%s AND status='pending' RETURNING id",
                (status, code, message, self.rid, step["id"]),
            ).fetchone()
            if row and step["step_type"] == "rest":
                self.stats_transition(db, step.get("parent_step_id"), "pending", status)
            for parameter in self.sources.get(step["id"], []):
                db.execute(
                    "UPDATE task_run_parameters SET status='skipped',finished_at=now(),error_code=%s WHERE run_id=%s AND parameter_id=%s AND status='pending'",
                    (code, self.rid, parameter["id"]),
                )

    def leaf(self, config, step, values, abort=None):
        with connection() as db:
            row = db.execute("SELECT step_run_id FROM task_run_steps WHERE run_id=%s AND step_id=%s",
                             (self.rid, step["id"])).fetchone()
        token = step_context.set({"runId": row["step_run_id"], "task_run_id": self.rid,
                                  "task_step_id": step["id"]})
        try:
            LOGGER.info("步骤开始", extra={"stage": "step"})
            return self._leaf(config, step, values, abort)
        finally:
            LOGGER.info("步骤结束", extra={"stage": "step"})
            step_context.reset(token)

    def _leaf(self, config, step, values, abort=None):
        """A child owns its context, DB connections and HTTP permit for its whole lifecycle."""
        parent = step.get("parent_step_id")
        slot, qid, response, duration, started = (
            False,
            None,
            None,
            None,
            time.monotonic(),
        )
        writes = {}
        update_names = {a["name"] for a in (step.get("options") or {}).get("assignments", [])}
        error = None
        stage = "condition"
        try:
            action = condition_action(step, values)
            if action == "skip":
                self.skip_node(step, "CONDITION_NOT_MET")
                return {}, None
            if action == "blocked":
                raise BusinessError(422, "PRECONDITION_BLOCKED", "节点前提不足，执行阻塞")
            if step["step_type"] == "rest":
                while not HTTP_SLOTS.acquire(timeout=0.1):
                    if self.stop.is_set() or (abort and abort.is_set()):
                        break
                else:
                    slot = True
            if self.stop.is_set():
                raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
            if abort and abort.is_set():
                self.skip_node(step, "PARALLEL_ABORTED")
                if slot:
                    HTTP_SLOTS.release()
                return {}, None
            if step["step_type"] == "rest" and not slot:
                raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
            stage = "request_build"
            request = check_inputs(config, step, values) if step["step_type"] == "rest" else None
            started = time.monotonic()
            with connection() as db:
                self.check_active(db)
                node = db.execute(
                    "UPDATE task_run_steps SET status='running',started_at=now() WHERE run_id=%s AND step_id=%s AND status='pending' RETURNING id",
                    (self.rid, step["id"]),
                ).fetchone()
                if not node:
                    raise BusinessError(422, "EXECUTION_INTERRUPTED", "节点已经执行，禁止重复发送")
                if request:
                    self.stats_transition(db, parent, "pending", "running", sent=parent is not None)
                    if parent is None:
                        phase = (
                            "finally"
                            if step["execution_scope"] == "finally"
                            else step.get("execution_phase", "steps")
                        )
                        qid = db.execute(
                            "INSERT INTO task_requests(run_id,run_step_id,step_id,position,method,actual_url,request_headers,request_body,phase) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (
                                self.rid,
                                node["id"],
                                step["id"],
                                step["position"],
                                request["method"],
                                request["actual_url"],
                                Jsonb(request["request_headers"]),
                                request["request_body"],
                                phase,
                            ),
                        ).fetchone()["id"]
                for p in self.sources.get(step["id"], []):
                    if p["name"] in update_names:
                        continue
                    db.execute(
                        "UPDATE task_run_parameters SET status='running',started_at=now(),request_id=%s WHERE run_id=%s AND parameter_id=%s",
                        (qid, self.rid, p["id"]),
                    )
            # No open DB transaction or connection while waiting/sending/asserting.
            if request:
                stage = "rest"
                LOGGER.info("发起 REST 请求", extra={"stage": stage})
                response = request_http(request, config.get("timeout_seconds", 30))
                LOGGER.info("REST 请求完成", extra={"stage": stage, "status": response.get("response_status"), "code": response.get("error_code")})
                duration = request["duration_ms"]
                stage = "extraction"
                LOGGER.info("提取全局参数", extra={"stage": stage})
                writes = extract_values(config, step, response)
                stage = "assertion"
                LOGGER.info("执行业务断言", extra={"stage": stage})
                response = apply_assertion(step, response, copy.deepcopy(values))
                if response["error_code"]:
                    raise BusinessError(422, response["error_code"], response["error_message"])
            elif step["step_type"] == "compute":
                stage = "compute"
                writes = compute_values(config, step, values)
            else:
                stage = "wait"
                wait_until(wait_milliseconds(step, values), self.stop)
        except Exception as exc:  # noqa: BLE001 - persist a safe terminal result on every path
            code, message = (
                (exc.payload["code"], exc.payload["detail"])
                if isinstance(exc, BusinessError)
                else ("EXECUTION_ERROR", "节点执行内部错误")
            )
            if step["execution_scope"] == "finally" and code == "PARAMETER_NOT_AVAILABLE":
                code, message = "CLEANUP_INPUT_UNAVAILABLE", "清理所需参数尚未发布，未发送请求"
            context = step_context.get()
            message = f"步骤 {step['id']} · runId={context.get('runId', '-')} · 阶段={stage}\n{message}"
            LOGGER.error(message, extra={"stage": stage, "code": code},
                         exc_info=not isinstance(exc, BusinessError))
            error = (code, message)
            if abort:
                abort.set()
        try:
            with connection() as db:
                row = db.execute(
                    "SELECT * FROM task_run_steps WHERE run_id=%s AND step_id=%s FOR UPDATE",
                    (self.rid, step["id"]),
                ).fetchone()
                if row["status"] not in ("pending", "running"):
                    return {}, error
                status = (
                    ("blocked" if error[0] == "PRECONDITION_BLOCKED" else "failed")
                    if error
                    else "succeeded"
                )
                message = error[1] if error else None
                db.execute(
                    "UPDATE task_run_steps SET status=%s,finished_at=now(),duration_ms=%s,error_code=%s,error_message=%s WHERE id=%s",
                    (
                        status,
                        None
                        if parent is not None
                        else max(0, int((time.monotonic() - started) * 1000)),
                        error[0] if error else None,
                        message,
                        row["id"],
                    ),
                )
                if step["step_type"] == "rest":
                    self.stats_transition(db, parent, row["status"], status, response, duration)
                if qid:
                    r = response or {}
                    db.execute(
                        "UPDATE task_requests SET response_status=%s,response_headers=%s,response_body=%s,response_body_encoding=%s,error_code=%s,error_message=%s,finished_at=now(),duration_ms=%s WHERE id=%s",
                        (
                            r.get("response_status"),
                            Jsonb(r["response_headers"])
                            if r.get("response_headers") is not None
                            else None,
                            r.get("response_body"),
                            r.get("response_body_encoding"),
                            error[0] if error else None,
                            error[1] if error else None,
                            duration,
                            qid,
                        ),
                    )
                for p in self.sources.get(step["id"], []):
                    assigned = p["name"] in writes
                    if not assigned and p["name"] in update_names:
                        continue
                    db.execute(
                        "UPDATE task_run_parameters SET status=%s,value=%s,published=%s,request_id=%s,finished_at=now(),duration_ms=CASE WHEN started_at IS NOT NULL THEN (extract(epoch FROM (now()-started_at))*1000)::bigint ELSE NULL END,error_code=%s,error_message=%s WHERE run_id=%s AND parameter_id=%s",
                        (
                            "succeeded" if assigned else status,
                            Jsonb(writes[p["name"]]) if assigned else None,
                            assigned and parent is None,
                            qid,
                            error[0] if error and not assigned else None,
                            error[1] if error and not assigned and parent is None else None,
                            self.rid,
                            p["id"],
                        ),
                    )
            return writes, error
        finally:
            if slot:
                HTTP_SLOTS.release()

    def parallel(self, config, step):
        with connection() as db:
            row = db.execute("SELECT step_run_id FROM task_run_steps WHERE run_id=%s AND step_id=%s",
                             (self.rid, step["id"])).fetchone()
        token = step_context.set({"runId": row["step_run_id"], "task_run_id": self.rid,
                                  "task_step_id": step["id"]})
        try:
            LOGGER.info("并发步骤开始", extra={"stage": "parallel"})
            result = self._parallel(config, step)
            if result:
                LOGGER.error(result[1], extra={"stage": "parallel", "code": result[0]})
            return result
        finally:
            LOGGER.info("并发步骤结束", extra={"stage": "parallel"})
            step_context.reset(token)

    def _parallel(self, config, step):
        children = siblings(config, parent=step["id"])
        action = condition_action(step, self.values)
        if action:
            code = "PRECONDITION_BLOCKED" if action == "blocked" else "CONDITION_NOT_MET"
            self.skip_node(step, code, "blocked" if action == "blocked" else "skipped")
            for child in children:
                self.skip_node(child, code)
            return (code, "并发节点前提不足") if action == "blocked" else None
        abort = threading.Event()
        started = time.monotonic()
        with connection() as db:
            self.check_active(db)
            db.execute(
                "UPDATE task_run_steps SET status='running',started_at=now() WHERE run_id=%s AND step_id=%s",
                (self.rid, step["id"]),
            )
        iterator, pending, outputs, errors, launched = iter(children), {}, {}, [], set()

        def submit():
            child = next(iterator, None)
            if child is not None:
                launched.add(child["id"])
                pending[
                    CHILD_POOL.submit(self.leaf, config, child, copy.deepcopy(self.values), abort)
                ] = child
                return True
            return False

        for _ in range(step["max_concurrency"]):
            if not abort.is_set() and not self.stop.is_set():
                submit()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                child = pending.pop(future)
                try:
                    writes, error = future.result()
                except Exception:  # noqa: BLE001 - still wait for every started child
                    abort.set()
                    writes, error = {}, ("EXECUTION_ERROR", "并发节点执行内部错误")
                outputs.update(writes)
                if error:
                    errors.append((child["position"], child["id"], error))
            if not abort.is_set() and not self.stop.is_set():
                while len(pending) < step["max_concurrency"] and submit():
                    pass
        for child in children:
            if child["id"] not in launched:
                self.skip_node(child, "PARALLEL_ABORTED")
        if self.stop.is_set():
            raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
        error = None
        if errors:
            _position, sid, child_error = min(errors)
            error = (
                "PRECONDITION_BLOCKED"
                if child_error[0] == "PRECONDITION_BLOCKED"
                else "PARALLEL_FAILED",
                f"并发子节点 {sid} 未成功（{child_error[0]}）\n{child_error[1]}",
            )
        with connection() as db:
            self.check_active(db)
            db.execute(
                "UPDATE task_run_steps SET status=%s,finished_at=now(),duration_ms=%s,error_code=%s,error_message=%s WHERE run_id=%s AND step_id=%s",
                (
                    ("blocked" if error[0] == "PRECONDITION_BLOCKED" else "failed")
                    if error
                    else "succeeded",
                    max(0, int((time.monotonic() - started) * 1000)),
                    error[0] if error else None,
                    error[1] if error else None,
                    self.rid,
                    step["id"],
                ),
            )
            db.execute(
                "UPDATE task_run_parameters SET published=true WHERE run_id=%s AND status='succeeded' AND parameter_id=ANY(%s)",
                (
                    self.rid,
                    [p["id"] for child in children for p in self.sources.get(child["id"], [])],
                ),
            )
        self.values.update(outputs)
        return error

    def execute(self):
        config = None
        try:
            with connection() as db:
                row = db.execute("SELECT * FROM task_runs WHERE id=%s", (self.rid,)).fetchone()
                if not row or row["status"] != "running":
                    return
                config = row["config_snapshot"]
                if "collection_run_id" in config:
                    inherited = db.execute(
                        "SELECT input_parameters FROM task_collection_run_members WHERE task_run_id=%s",
                        (self.rid,),
                    ).fetchone()["input_parameters"]
                    fresh = {
                        p["name"]
                        for p in config["global_parameters"]
                        if p["source_type"] == "expression"
                    }
                    self.values = {
                        n: copy.deepcopy(p["value"]) for n, p in inherited.items() if n not in fresh
                    }
            self.sources = parameter_sources(config)
            primary = None
            try:
                for p in config["global_parameters"]:
                    if p["source_type"] != "fixed":
                        continue
                    if not TYPES[p["value_type"]](p.get("value")):
                        with connection() as db:
                            db.execute(
                                "UPDATE task_run_parameters SET status='failed',finished_at=now(),error_code='PARAMETER_INITIALIZATION_FAILED' WHERE run_id=%s AND parameter_id=%s",
                                (self.rid, p["id"]),
                            )
                        raise BusinessError(
                            422, "PARAMETER_INITIALIZATION_FAILED", "固定参数初始化失败"
                        )
                    with connection() as db:
                        self.check_active(db)
                        db.execute(
                            "UPDATE task_run_parameters SET status='succeeded',value=%s,published=true,started_at=now(),finished_at=now(),duration_ms=0 WHERE run_id=%s AND parameter_id=%s",
                            (Jsonb(p["value"]), self.rid, p["id"]),
                        )
                    self.values[p["name"]] = copy.deepcopy(p["value"])
                for step in siblings(config):
                    if step["step_type"] == "parallel":
                        error = self.parallel(config, step)
                    else:
                        writes, error = self.leaf(config, step, self.values)
                        self.values.update(writes)
                    if error:
                        raise BusinessError(422, *error)
            except Exception as exc:  # noqa: BLE001 - defer terminal state until cleanup
                primary = (
                    (exc.payload["code"], exc.payload["detail"])
                    if isinstance(exc, BusinessError)
                    else ("EXECUTION_ERROR", "主流程执行内部错误")
                )
            if self.stop.is_set() or (primary and primary[0] == "EXECUTION_INTERRUPTED"):
                raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
            cleanup = siblings(config, "finally")
            with connection() as db:
                self.check_active(db)
                db.execute(
                    "UPDATE task_runs SET primary_error_code=%s,primary_error_message=%s,execution_stage=%s,cleanup_status=%s WHERE id=%s",
                    (
                        primary[0] if primary else None,
                        primary[1] if primary else None,
                        "finally" if cleanup else "finished",
                        "running" if cleanup else "skipped",
                        self.rid,
                    ),
                )
            with connection() as db:
                db.execute(
                    "UPDATE task_run_steps SET status='skipped',finished_at=now(),error_code='UPSTREAM_FAILED',"
                    "error_message='主流程已停止' WHERE run_id=%s AND execution_scope='main' AND status='pending'",
                    (self.rid,),
                )
                db.execute(
                    "UPDATE task_run_parameters SET status='skipped',finished_at=now(),error_code='UPSTREAM_FAILED' "
                    "WHERE run_id=%s AND status='pending' AND parameter_id=ANY(%s)",
                    (
                        self.rid,
                        [
                            p["id"]
                            for s in config["steps"]
                            if s["execution_scope"] == "main"
                            for p in self.sources.get(s["id"], [])
                        ],
                    ),
                )
                parents = db.execute(
                    "SELECT id,parallel_stats FROM task_run_steps WHERE run_id=%s AND step_type='parallel' FOR UPDATE",
                    (self.rid,),
                ).fetchall()
                for parent in parents:
                    stats = parent["parallel_stats"]
                    if stats["pending_count"]:
                        stats["skipped_count"] += stats["pending_count"]
                        stats["pending_count"] = 0
                        db.execute(
                            "UPDATE task_run_steps SET parallel_stats=%s WHERE id=%s",
                            (Jsonb(stats), parent["id"]),
                        )
            cleanup_failed = False
            for step in cleanup:
                writes, error = self.leaf(config, step, self.values)
                self.values.update(writes)
                cleanup_failed |= error is not None
                if error:
                    with connection() as db:
                        db.execute(
                            "UPDATE task_runs SET cleanup_status='failed' WHERE id=%s", (self.rid,)
                        )
                if self.stop.is_set():
                    raise BusinessError(422, "EXECUTION_INTERRUPTED", "执行中断")
            final = primary or (("CLEANUP_FAILED", "清理步骤失败") if cleanup_failed else None)
            with connection() as db:
                self.check_active(db)
                db.execute(
                    "UPDATE task_run_parameters SET status='skipped',finished_at=now(),error_code='UPSTREAM_FAILED' WHERE run_id=%s AND status='pending'",
                    (self.rid,),
                )
                db.execute(
                    "UPDATE task_runs SET status=%s,execution_stage='finished',cleanup_status=%s,error_code=%s,error_message=%s,finished_at=now(),duration_ms=(extract(epoch FROM (now()-started_at))*1000)::bigint WHERE id=%s",
                    (
                        (
                            "blocked"
                            if primary
                            and primary[0] == "PRECONDITION_BLOCKED"
                            and not cleanup_failed
                            else "failed"
                        )
                        if final
                        else "succeeded",
                        ("failed" if cleanup_failed else "succeeded") if cleanup else "skipped",
                        final[0] if final else None,
                        final[1] if final else None,
                        self.rid,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - stop/recovery paths never send cleanup requests
            code, message = (
                (exc.payload["code"], exc.payload["detail"])
                if isinstance(exc, BusinessError)
                else ("EXECUTION_ERROR", "执行内部错误")
            )
            try:
                with connection() as db:
                    finish_failure(db, self.rid, code, message)
            except Exception:  # noqa: BLE001
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
                        from app.service.task_collections import recover

                        recover(db, self.created_at)
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
                        from app.service.task_collections import advance, flush_audits

                        with db.transaction():
                            advance(db)
                        with db.transaction():
                            flush_audits(db)
                        if len(self.workers) < 8:
                            with db.transaction():
                                row = db.execute(
                                    "SELECT r.id FROM task_runs r WHERE r.status='queued' AND ("
                                    "NOT EXISTS(SELECT 1 FROM task_collection_run_members m "
                                    "WHERE m.task_run_id=r.id) OR EXISTS("
                                    "SELECT 1 FROM task_collection_run_members m "
                                    "JOIN task_collection_runs c ON c.id=m.collection_run_id "
                                    "WHERE m.task_run_id=r.id AND m.status='pending' AND m.ready "
                                    "AND c.status='running' AND NOT EXISTS("
                                    "SELECT 1 FROM task_collection_run_members peer "
                                    "JOIN task_runs pr ON pr.id=peer.task_run_id "
                                    "WHERE peer.collection_run_id=c.id AND peer.position=m.position "
                                    "AND (pr.status IN ('failed','blocked') OR pr.primary_error_code IS NOT NULL OR pr.cleanup_status='failed')))) "
                                    "ORDER BY r.created_at,r.id FOR UPDATE OF r SKIP LOCKED LIMIT 1"
                                ).fetchone()
                                if row:
                                    db.execute(
                                        "UPDATE task_runs SET status='running',started_at=now() WHERE id=%s",
                                        (row["id"],),
                                    )
                                    db.execute(
                                        "UPDATE task_collection_run_members SET status='running' "
                                        "WHERE task_run_id=%s",
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
