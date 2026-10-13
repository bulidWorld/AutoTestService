from fastapi.responses import JSONResponse

from app.service.health import readiness


async def health():
    return {"status": "ok"}


def ready():
    available = readiness()
    return JSONResponse(
        status_code=200 if available else 503,
        content={"status": "ok" if available else "unavailable"},
    )
