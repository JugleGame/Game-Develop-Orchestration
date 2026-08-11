"""Structured (JSON) logging setup.

``print()`` is forbidden project-wide; all diagnostic output must go through
the loggers configured here so it can be shipped to a log aggregator.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config.settings import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Configure the root logger exactly once for the process."""

    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stdout)]

    if settings.log_file:
        # Same JSON-per-line format as stdout, so a shipper (Loki/Promtail,
        # Filebeat) can tail the file with no extra parsing rules.
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=settings.log_file_max_bytes,
                backupCount=settings.log_file_backup_count,
                encoding="utf-8",
            )
        )

    formatter = JsonFormatter()
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers = handlers

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger, configuring logging on first use."""

    configure_logging()
    return logging.getLogger(name)


def log_extra(**fields: Any) -> dict[str, Any]:
    """Build the ``extra`` mapping needed to attach structured fields.

    Usage: ``logger.info("mcp call", extra=log_extra(game_id=game_id))``
    """

    return {"extra_fields": fields}
