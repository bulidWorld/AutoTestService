import logging
from pathlib import Path

import yaml
from fastapi.responses import JSONResponse
from openapi_spec_validator import validate

from app.api.business import bind
from app.api.registry import CONTRACT_IMPLEMENTATIONS

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def unimplemented(path, method):
    # Factory binds each route separately and avoids spurious args/kwargs query parameters.
    async def endpoint():
        return JSONResponse(status_code=501, content={"detail": f"接口 {path} [{method}] 暂未实现"})

    return endpoint


def register_contract(app, path: Path):
    try:
        specification = yaml.safe_load(path.read_text(encoding="utf-8"))
        validate(specification)
    except Exception as exc:
        logging.getLogger(__name__).exception("OpenAPI 契约加载或校验失败")
        raise ValueError("OpenAPI 契约加载或校验失败，请检查 YAML") from exc
    merged = getattr(app, "contract_specification", None)
    if merged is None:
        import copy

        app.contract_specification = copy.deepcopy(specification)
    else:
        if set(merged["paths"]) & set(specification["paths"]):
            raise ValueError("契约路径重复")
        # Components are namespaced when different contracts share schema names.
        import copy

        prefix = path.stem.replace("-", "_") + "_"
        renamed = copy.deepcopy(specification)

        def rewrite(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "$ref" and item.startswith("#/components/"):
                        parts = item.split("/")
                        value[key] = "/".join(parts[:-1] + [prefix + parts[-1]])
                    else:
                        rewrite(item)
            elif isinstance(value, list):
                for item in value:
                    rewrite(item)

        rewrite(renamed)
        merged["paths"].update(renamed["paths"])
        for category, definitions in renamed.get("components", {}).items():
            merged.setdefault("components", {}).setdefault(category, {}).update(
                {prefix + key: value for key, value in definitions.items()}
            )
        merged.setdefault("tags", []).extend(renamed.get("tags", []))
    app.openapi = lambda: app.contract_specification
    implementation = CONTRACT_IMPLEMENTATIONS.get(path.stem)
    for route, definition in specification["paths"].items():
        for method, operation in definition.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            handler = implementation.OPERATIONS.get(operation_id) if implementation else None
            if handler is None:
                endpoint = unimplemented(route, method.upper())
            elif operation_id in implementation.SYSTEM_OPERATIONS:
                endpoint = handler
            else:
                endpoint = bind(operation, specification, handler)
            app.add_api_route(
                route,
                endpoint,
                methods=[method.upper()],
                operation_id=operation_id,
                summary=operation.get("summary"),
                description=operation.get("description"),
            )
