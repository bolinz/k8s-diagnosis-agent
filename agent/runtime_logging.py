from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", ""),
            "message": record.getMessage(),
        }
        extra = getattr(record, "payload", {})
        if isinstance(extra, dict):
            payload.update({key: value for key, value in extra.items() if value is not None})
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def setup_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    for logger_name in ("kubernetes", "urllib3", "asyncio", "httpx"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def get_logger(component: str) -> logging.Logger:
    logger = logging.getLogger(component)
    logger.propagate = True
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **payload: Any,
) -> None:
    logger.log(
        level,
        message,
        extra={
            "component": logger.name,
            "event": event,
            "payload": sanitize_payload(payload),
        },
    )


def truncate_text(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated>"


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if "key" in key.lower() or "secret" in key.lower() or "token" in key.lower():
            continue
        if isinstance(value, str):
            sanitized[key] = truncate_text(value)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_payload(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_payload(item) if isinstance(item, dict) else truncate_text(item) if isinstance(item, str) else item
                for item in value[:20]
            ]
        else:
            sanitized[key] = value
    return sanitized
