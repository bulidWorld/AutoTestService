"""Configuration paths are resolved against the deployment root."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def absolute(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def load_settings() -> dict:
    try:
        base = yaml.safe_load((ROOT / "config/application.yaml").read_text())
        override = os.environ.get("APP_CONFIG")
        if override:
            base = merge(base, yaml.safe_load(absolute(override).read_text()) or {})
        if not isinstance(base, dict):
            raise TypeError("配置必须为键值结构")
        service = base["service"]
        if not isinstance(service["port"], int) or not 1 <= service["port"] <= 65535:
            raise ValueError("服务端口必须在 1～65535 之间")
        if not service["host"] or service["startup_timeout"] <= 0 or service["stop_timeout"] <= 0:
            raise ValueError("服务地址或超时无效")
        database = base["database"]
        if not database["name"] or not database["user"] or database["connect_timeout"] <= 0:
            raise ValueError("数据库配置无效")
        if not 1 <= database["port"] <= 65535:
            raise ValueError("数据库端口无效")
        log = base["logging"]
        if log["max_bytes"] <= 0 or log["backup_count"] <= 0:
            raise ValueError("日志轮转大小及保留数必须为正数")
        if not absolute(base["contract"]).is_file():
            raise ValueError(f"找不到 OpenAPI 契约文件：{absolute(base['contract'])}")
        return base
    except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("找不到 OpenAPI 契约文件："):
            raise
        raise ValueError("应用配置无效，请检查配置文件、端口及必填项") from exc
