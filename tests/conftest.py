# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Pytest fixtures: an app on an in-memory database, a fake calendar feed and a fake outbox."""

from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from justbookmycal import availability, invite
from justbookmycal.app import create_app
from justbookmycal.extensions import db

FIXTURES = Path(__file__).parent / "fixtures"
# Monday 08:00 in Berlin (CEST). The clocks go back on Sunday 2026-10-25.
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)

TEST_CONFIG = {
    "TESTING": True,
    "SECRET_KEY": "x" * 32,
    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    "WTF_CSRF_ENABLED": False,
    "RATELIMIT_ENABLED": False,
    "SESSION_COOKIE_SECURE": False,
    "TRUSTED_PROXIES": 0,
    "OWNER_NAME": "Max Mehl",
    "OWNER_EMAIL": "max@example.com",
    "SYSTEM_EMAIL": "noreply@example.com",
    "TIMEZONE": "Europe/Berlin",
    "ICS_FEEDS": ["https://cal.example.com/max.ics"],
    "SMTP_HOST": "smtp.example.com",
    "MIN_NOTICE_HOURS": 0,
}


class Feed:
    """The fake calendar feed. Set `data` or `error` to change what the app downloads."""

    def __init__(self) -> None:
        self.data: bytes = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nEND:VCALENDAR\r\n"
        self.error: Exception | None = None
        self.downloads = 0

    def download(self, url: str, timeout: int) -> bytes:
        """Stand-in for availability._download."""
        self.downloads += 1
        if self.error:
            raise self.error
        return self.data


@pytest.fixture(name="feed", autouse=True)
def fixture_feed(monkeypatch: pytest.MonkeyPatch) -> Feed:
    """Replace the network, start every test with an empty cache and a fixed clock."""
    fake = Feed()
    monkeypatch.setattr(availability, "_download", fake.download)
    monkeypatch.setattr(availability, "utcnow", lambda: NOW)
    availability._cache.clear()
    return fake


@pytest.fixture(name="outbox")
def fixture_outbox(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    """Collect the mails that would be sent."""
    sent: list[EmailMessage] = []
    monkeypatch.setattr(invite, "_send", lambda cfg, messages: sent.extend(messages) or True)
    return sent


@pytest.fixture(name="app")
def fixture_app():
    """An app on an empty in-memory database."""
    app = create_app(config_overrides=dict(TEST_CONFIG))
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture(name="client")
def fixture_client(app: Flask) -> FlaskClient:
    """Test client."""
    return app.test_client()
