"""Structured logging to stdout/stderr (journald captures it under systemd).

Every message is a short human sentence followed by ``key=value`` context, e.g.::

    INFO  VM synchronized vmid=101 name=web01 changes=memory,primary_ip4

Secrets known to the configuration are masked by :class:`RedactingFilter`
in case they ever end up in an exception message or URL.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_LOGGER_NAME = "netbox_pve_sync"
_TOKEN_PATTERNS = [
    re.compile(r"(Authorization:\s*(?:Token|Bearer)\s+)\S+", re.IGNORECASE),
    re.compile(r"(PVEAPIToken=[^=\s]+=)\S+", re.IGNORECASE),
    re.compile(r"(nbt_[A-Za-z0-9]+\.)\S+"),
]


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def mask_secret(secret: str | None) -> str:
    """Return a safe hint such as ``abcd…(40 chars)`` for showing which token is configured."""
    if not secret:
        return "<not set>"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…({len(secret)} chars)"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***REDACTED***")
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(r"\1***REDACTED***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            record.ctx = {k: self.redact(str(v)) if isinstance(v, str) else v for k, v in ctx.items()}
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _fmt_value(value: Any) -> str:
    text = ",".join(str(v) for v in value) if isinstance(value, (list, tuple, set)) else str(value)
    if text == "" or any(c in text for c in " \"="):
        return json.dumps(text)
    return text


class TextFormatter(logging.Formatter):
    def __init__(self, with_time: bool) -> None:
        super().__init__()
        self.with_time = with_time

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        if self.with_time:
            parts.append(self.formatTime(record, "%Y-%m-%dT%H:%M:%S"))
        parts.append(f"{record.levelname:<7}")
        parts.append(record.getMessage())
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            parts.append(" ".join(f"{k}={_fmt_value(v)}" for k, v in ctx.items() if v is not None))
        line = " ".join(parts)
        if record.exc_text:
            line += "\n" + record.exc_text
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            data.update({k: v for k, v in ctx.items() if v is not None})
        if record.exc_text:
            data["exception"] = record.exc_text
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO", fmt: str = "text", secrets: list[str] | None = None) -> None:
    """Configure the package logger. Under systemd, journald adds its own timestamps."""
    import os

    under_journald = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter(with_time=not under_journald))
    root = logging.getLogger(_LOGGER_NAME)
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    # urllib3 is noisy about unverified HTTPS; we log our own explicit warning instead.
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def log(logger: logging.Logger, level: int, message: str, *, exc_info: bool = False, **ctx: Any) -> None:
    """Log ``message`` with structured ``key=value`` context."""
    logger.log(level, message, exc_info=exc_info, extra={"ctx": ctx})
