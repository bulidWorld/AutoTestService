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


def load_contract(path: Path):
    """Bundle local component references into a self-contained OpenAPI document."""
    import copy

    path = path.resolve()
    documents = {path: yaml.safe_load(path.read_text(encoding="utf-8"))}
    specification = copy.deepcopy(documents[path])
    imported = {}

    def rewrite(value, source):
        if isinstance(value, list):
            return [rewrite(item, source) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key != "$ref" or not isinstance(item, str):
                result[key] = rewrite(item, source)
                continue
            filename, separator, pointer = item.partition("#")
            if not separator or not pointer.startswith("/components/"):
                raise ValueError("契约引用必须指向本地组件")
            parts = pointer.split("/")
            if len(parts) != 4:
                raise ValueError("契约引用必须指向完整组件")
            category, name = parts[2:]
            if filename and (":" in filename or filename.startswith("/")):
                raise ValueError("契约引用仅支持相对文件路径")
            target = (source.parent / filename).resolve() if filename else source
            if target == path:
                result[key] = "#" + pointer
                continue
            identity = (target, category, name)
            if identity not in imported:
                bundled_name = target.stem.replace("-", "_") + "_" + name
                definitions = specification.setdefault("components", {}).setdefault(category, {})
                if bundled_name in definitions:
                    raise ValueError("契约组件名称冲突")
                imported[identity] = bundled_name
                if target not in documents:
                    documents[target] = yaml.safe_load(target.read_text(encoding="utf-8"))
                definitions[bundled_name] = {}
                definitions[bundled_name] = rewrite(
                    documents[target]["components"][category][name], target
                )
            result[key] = f"#/components/{category}/{imported[identity]}"
        return result

    # Work on a snapshot: imports add new definitions while original definitions are rewritten.
    rewritten = rewrite(copy.deepcopy(specification), path)
    for category, definitions in specification.get("components", {}).items():
        for name, definition in definitions.items():
            if name not in rewritten.get("components", {}).get(category, {}):
                rewritten.setdefault("components", {}).setdefault(category, {})[name] = definition
    validate(rewritten)
    return rewritten


def register_contract(app, path: Path):
    try:
        specification = load_contract(path)
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
                endpoint = bind(
                    operation,
                    specification,
                    handler,
                    database_free=operation_id
                    in getattr(implementation, "DATABASE_FREE_OPERATIONS", set()),
                )
            app.add_api_route(
                route,
                endpoint,
                methods=[method.upper()],
                operation_id=operation_id,
                summary=operation.get("summary"),
                description=operation.get("description"),
            )
