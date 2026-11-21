"""One logging setup for the dashboard and the updater.

Loguru starts with a DEBUG-level stderr sink. Under the scheduled tasks the
dashboard's stderr is redirected into data/logs/dashboard.log, so that
default sink copied every app.log line into dashboard.log too (and at DEBUG,
ignoring LOG_LEVEL). configure_logging() replaces it: the log file always,
plus a console sink at the same level only when stderr is an interactive
terminal. dashboard.log keeps what only it can capture - Streamlit's own
output and uncaught tracebacks.
"""
import sys

from loguru import logger

from app.config.settings import settings


def configure_logging(log_file: str) -> None:
    """Replace loguru's default sinks with the app's file (+ TTY console) sinks."""
    logger.remove()
    logger.add(log_file, rotation="10 MB", retention="30 days", level=settings.log_level)
    stream = sys.stderr
    if stream is not None and hasattr(stream, "isatty") and stream.isatty():
        logger.add(stream, level=settings.log_level)
