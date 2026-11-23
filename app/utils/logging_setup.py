"""One logging setup for the dashboard and the updater.

Loguru starts with a DEBUG-level stderr sink. Under the scheduled tasks the
dashboard's stderr is redirected into data/logs/dashboard.log, so that
default sink copied every app.log line into dashboard.log too (and at DEBUG,
ignoring LOG_LEVEL). configure_logging() replaces it: the log file always,
plus a console sink at the same level only when stderr is an interactive
terminal. dashboard.log keeps what only it can capture - Streamlit's own
output and uncaught tracebacks.

Every message also passes redact_secrets(): the value of any setting whose
name contains key/secret/password/token is replaced before it is written.
A requests error once echoed the Twelve Data request URL - apikey included
- 583 times into app.log (Aug-Sep 2026); this stops any code path doing it.
"""
import sys

from loguru import logger

from app.config.settings import settings

_SECRET_NAME_PARTS = ("key", "secret", "password", "token", "pwd")
_MIN_SECRET_LEN = 8          # don't "redact" short values like "true" or a port
REDACTED = "<redacted>"


def secret_values() -> list[str]:
    """Configured secret values, longest first (a key containing another wins)."""
    values = set()
    for name, value in settings.model_dump().items():
        if any(part in name.lower() for part in _SECRET_NAME_PARTS) and isinstance(value, str):
            if len(value) >= _MIN_SECRET_LEN:
                values.add(value)
    return sorted(values, key=len, reverse=True)


def redact_secrets(text: str, secrets: list[str]) -> str:
    for value in secrets:
        if value in text:
            text = text.replace(value, REDACTED)
    return text


def configure_logging(log_file: str) -> None:
    """Replace loguru's default sinks with the app's file (+ TTY console) sinks."""
    secrets = secret_values()

    def _patch(record):
        record["message"] = redact_secrets(record["message"], secrets)

    logger.remove()
    logger.configure(patcher=_patch)
    logger.add(log_file, rotation="10 MB", retention="30 days", level=settings.log_level)
    stream = sys.stderr
    if stream is not None and hasattr(stream, "isatty") and stream.isatty():
        logger.add(stream, level=settings.log_level)
