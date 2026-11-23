"""Configured secrets never reach a log file."""
from loguru import logger

from app.utils import logging_setup
from app.utils.logging_setup import REDACTED, configure_logging, redact_secrets, secret_values


def test_secret_settings_are_collected_but_short_or_non_secret_values_are_not(monkeypatch):
    class FakeSettings:
        log_level = "INFO"
        def model_dump(self):
            return {"twelve_data_api_key": "a63fakekey123456", "easyequities_password": "hunter2hunter2",
                    "luno_api_secret": "s3cr3tvalue", "database_url": "postgresql://x", "notification_enabled": True,
                    "gold_api_key": "", "short_token": "abc"}
    monkeypatch.setattr(logging_setup, "settings", FakeSettings())
    assert set(secret_values()) == {"a63fakekey123456", "hunter2hunter2", "s3cr3tvalue"}


def test_redaction_replaces_every_occurrence():
    msg = "404 for url: https://api.twelvedata.com/quote?symbol=X&apikey=a63fakekey123456 (a63fakekey123456)"
    assert redact_secrets(msg, ["a63fakekey123456"]) == (
        f"404 for url: https://api.twelvedata.com/quote?symbol=X&apikey={REDACTED} ({REDACTED})")


def test_configured_logging_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, "secret_values", lambda: ["a63fakekey123456"])
    log = tmp_path / "app.log"
    configure_logging(str(log))
    try:
        logger.error("Error making request: 404 Client Error for url: https://x/quote?apikey=a63fakekey123456")
    finally:
        logger.remove()
        logger.configure(patcher=None)
    text = log.read_text(encoding="utf-8")
    assert "a63fakekey123456" not in text and REDACTED in text
