"""Allowlisted JSON logging that avoids request bodies and credentials."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

# Authorization events may name the principal, engine resource, action, policy version, and
# reason code (threat model logging rules); never content, titles, tokens, or native ids.
_ALLOWED_FIELDS = frozenset(
    {
        "action",
        "allowed",
        "attempt",
        "duration_ms",
        "error_code",
        "event",
        "job_id",
        "method",
        "operation",
        "path",
        "policy_version",
        "principal_id",
        "reason_code",
        "resource_id",
        "status_code",
        "trace_id",
    }
)


# Libraries may put content such as query text into ordinary messages, so only their warnings
# and errors are kept. The engine's own records and the server's lifecycle records pass.
_ENGINE_LOGGERS = ("context_engine", "uvicorn")


class _EngineRecordFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        """Pass engine records and third-party warnings; drop third-party chatter."""

        name = record.name
        engine = any(name == item or name.startswith(f"{item}.") for item in _ENGINE_LOGGERS)
        return engine or record.levelno >= logging.WARNING


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """Serialize one record with only allowlisted structured fields."""

        value: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "safe_fields", {})
        value.update({key: fields[key] for key in sorted(fields) if key in _ALLOWED_FIELDS})
        return json.dumps(value, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    """Install the allowlisted JSON formatter once on the root logger."""

    root = logging.getLogger()
    root.setLevel(level)
    if any(getattr(handler, "_context_engine", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_EngineRecordFilter())
    handler._context_engine = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a standard logger configured by the process entrypoint."""

    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log an event while allowing only approved operational fields."""

    logger.info(event, extra={"safe_fields": {"event": event, **fields}})
