"""Logging setup.

Never logs the API key: nothing in this codebase passes the key to a logger,
and `configure_logging` installs a filter that redacts it if it ever appears.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings

_CONFIGURED = False


class _RedactAPIKey(logging.Filter):
    """Belt-and-braces: scrub the API key from any log record that carries it."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = get_settings().anthropic_api_key
        if key and len(key) > 8:
            try:
                msg = record.getMessage()
            except Exception:  # pragma: no cover - malformed record args
                return True
            if key in msg:
                record.msg = msg.replace(key, "***REDACTED***")
                record.args = ()
        return True


def configure_logging() -> None:
    """Idempotently configure root logging."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(_RedactAPIKey())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # The SDK's httpx logging is noisy at DEBUG and can echo request bodies.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
