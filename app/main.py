import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.contract import register_contract
from app.config.logging import (
    configure_bootstrap_logging,
    configure_logging,
    request_context,
    request_id,
)
from app.config.settings import absolute, load_settings


def create_app(contract_path=None):
    settings = load_settings()
    configure_logging(settings)
    from app.service.task_execution import Dispatcher

    @asynccontextmanager
    async def lifespan(app):
        dispatcher = Dispatcher()
        app.state.task_dispatcher = dispatcher
        if any(route.name == "startTaskRun" for route in app.routes):
            dispatcher.start()
        try:
            yield
        finally:
            dispatcher.close()

    application = FastAPI(title="AutoTestService", lifespan=lifespan)
    register_contract(application, absolute(contract_path or settings["contract"]))
    if contract_path is None and absolute(settings["contract"]).name == "rest-project.yaml":
        contract_directory = absolute(settings["contract"]).parent
        register_contract(application, contract_directory / "rest-task.yaml")
        register_contract(application, contract_directory / "rest-parameter.yaml")

    application.mount("/static", StaticFiles(directory=absolute("app/static")), name="static")

    @application.get("/", include_in_schema=False)
    async def project_page():
        return FileResponse(absolute("app/static/index.html"))

    @application.middleware("http")
    async def correlate_request(request, call_next):
        identifier = uuid4().hex
        token = request_id.set(identifier)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger(__name__).exception(
                "请求处理失败",
                extra={**request_context(request), "status": 500, "code": "INTERNAL_ERROR"},
            )
            response = JSONResponse(
                status_code=500,
                content={"code": "INTERNAL_ERROR", "detail": "内部服务错误", "issues": []},
            )
        try:
            response.headers["X-Request-ID"] = identifier
            # Never log query strings, headers, bodies or raw exception messages.
            logging.getLogger("access").info(
                "method=%s status=%s elapsed_ms=%.2f",
                request.method,
                response.status_code,
                (time.monotonic() - started) * 1000,
                extra={**request_context(request), "status": response.status_code},
            )
            return response
        finally:
            request_id.reset(token)

    return application


def main():
    configure_bootstrap_logging()
    try:
        settings = load_settings()
        application = create_app()
        uvicorn.run(
            application,
            host=settings["service"]["host"],
            port=settings["service"]["port"],
            log_config=None,
            access_log=False,
        )
    except Exception:
        logging.getLogger(__name__).exception("应用启动失败")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
