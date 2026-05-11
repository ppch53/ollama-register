from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "authorization",
    "cookie",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
)


def _looks_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS)


def redact_value(value: Any, *, field_name: str = "") -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            key: redact_value(item, field_name=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, field_name=field_name) for item in value]
    if isinstance(value, str) and _looks_sensitive(field_name):
        if len(value) <= 8:
            return "<redacted>"
        return f"<redacted len={len(value)}>"
    return value


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
            "step": getattr(record, "step", ""),
            "message": record.getMessage(),
            "context": redact_value(getattr(record, "context", {})),
        }
        return json.dumps(payload, ensure_ascii=False)


@dataclass(slots=True)
class StructuredRunLogger:
    logger: logging.Logger
    run_id: str
    artifacts_dir: Path

    def log(self, level: int, step: str, message: str, **context: Any) -> None:
        self.logger.log(
            level,
            message,
            extra={
                "run_id": self.run_id,
                "step": step,
                "context": context,
            },
        )

    def debug(self, step: str, message: str, **context: Any) -> None:
        self.log(logging.DEBUG, step, message, **context)

    def info(self, step: str, message: str, **context: Any) -> None:
        self.log(logging.INFO, step, message, **context)

    def warning(self, step: str, message: str, **context: Any) -> None:
        self.log(logging.WARNING, step, message, **context)

    def error(self, step: str, message: str, **context: Any) -> None:
        self.log(logging.ERROR, step, message, **context)


def configure_structured_logging(
    name: str,
    *,
    run_id: str | None = None,
    artifacts_root: str | Path = "artifacts",
    level: int = logging.INFO,
) -> StructuredRunLogger:
    resolved_run_id = run_id or uuid.uuid4().hex
    artifacts_dir = Path(artifacts_root) / resolved_run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)

    return StructuredRunLogger(
        logger=logger,
        run_id=resolved_run_id,
        artifacts_dir=artifacts_dir,
    )
