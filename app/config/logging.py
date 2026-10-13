"""JSON logging with request correlation and bounded log files."""

import json
import logging
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from app.config.settings import absolute

request_id = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "process": record.process,
            "thread": record.thread,
            "module": record.name,
            "request_id": request_id.get(),
            "message": record.getMessage(),
        }
        for field in ("operation", "route", "method", "status", "code", "detail", "issues"):
            if hasattr(record, field):
                data[field] = getattr(record, field)
        # Record code locations without exception text, source lines or local values.
        if record.exc_info:
            data["exception"] = record.exc_info[0].__name__
            data["stack"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(data, ensure_ascii=False)


def request_context(request):
    """Use the registered route template; never record user query/header/body values."""
    route = request.scope.get("route")
    return {
        "method": request.method,
        "route": getattr(route, "path", "<unmatched>"),
        "operation": getattr(route, "operation_id", None),
    }


def configure_bootstrap_logging():
    """Report configuration failures without echoing YAML contents or credentials."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter("auto-test-service"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def configure_logging(settings):
    config = settings["logging"]
    directory = absolute(config["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter(settings["service"]["name"])
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(config["level"])
    for filename, level in (("application.log", config["level"]), ("error.log", "ERROR")):
        handler = RotatingFileHandler(
            directory / filename,
            maxBytes=config["max_bytes"],
            backupCount=config["backup_count"],
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(level)
        root.addHandler(handler)
    access = logging.getLogger("access")
    for handler in access.handlers[:]:
        access.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        directory / "access.log",
        maxBytes=config["max_bytes"],
        backupCount=config["backup_count"],
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    access.addHandler(handler)
    access.propagate = False
