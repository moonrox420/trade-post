"""Structured logging with trace IDs. Never logs to stdout. Never logs secrets."""

from __future__ import annotations

import contextlib
import contextvars
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LogLevel, Settings

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)

_FORBIDDEN_SUBSTRINGS = (
    "api_key", "api-key", "apikey",
    "secret", "password", "passwd", "token",
    "authorization", "bearer",
)


class TraceIdFilter(logging.Filter):
    """Inject the current trace id (or '-' if unset) into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get() or "-"
        return True


def _scrub(value):
    if not isinstance(value, str):
        return value
    lower = value.lower()
    for token in _FORBIDDEN_SUBSTRINGS:
        idx = lower.find(token)
        while idx != -1:
            eq = value.find("=", idx)
            if eq != -1:
                end = len(value)
                for j in range(eq + 1, len(value)):
                    if value[j] in (" ", ",", "\n", "\t", ")", "]", "}"):
                        end = j
                        break
                value = value[: eq + 1] + "[REDACTED]" + value[end:]
                lower = value.lower()
            idx = lower.find(token, idx + 1)
    return value


class SecretScrubbingFilter(logging.Filter):
    """Best-effort scrubber for accidentally logged secrets. Defense in depth."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage().lower()
        except Exception:
            return True
        for token in _FORBIDDEN_SUBSTRINGS:
            if token in message and "=" in message:
                record.msg = _scrub(record.msg)
                record.args = ()
                break
        return True


def configure_logging(settings: Settings, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_trade_post_configured", False):
        return
    level = getattr(logging, settings.log_level.value)
    root.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | "
            "trace=%(trace_id)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    if log_file is None:
        log_file = Path("logs") / "trade_post.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=10, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(TraceIdFilter())
    file_handler.addFilter(SecretScrubbingFilter())
    root.addHandler(file_handler)
    for noisy in ("ccxt", "urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    setattr(root, "_trade_post_configured", True)
    root.info("logging configured level=%s file=%s", settings.log_level.value, log_file)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_trace_id() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex


@contextlib.contextmanager
def trace_context(trace_id: str | None = None):
    token = trace_id_var.set(trace_id or new_trace_id())
    try:
        yield trace_id_var.get()
    finally:
        trace_id_var.reset(token)
