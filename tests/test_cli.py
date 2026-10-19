# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CLI commands and the real feed download."""

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from justbookmycal import availability

from .conftest import FIXTURES, Feed


def test_busy_command_lists_blocks_in_the_owners_time_zone(app, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    result = app.test_cli_runner().invoke(args=["busy", "--days", "7"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert "Mon 2026-10-19 14:00 - 15:00  weekly" in lines  # the moved instance
    assert "Thu 2026-10-22 00:00 - 00:00  vacation" in lines
    assert "Fri 2026-10-23 15:00 - 16:00  utc-event" in lines
    assert not any("transparent" in line or "cancelled" in line for line in lines)


def test_slots_command_lists_bookable_slots(app, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    result = app.test_cli_runner().invoke(args=["slots", "--duration", "60"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0] == "Mon 2026-10-19 09:00 CEST"
    assert "Mon 2026-10-19 14:00 CEST" not in lines  # the moved weekly sync
    assert "Mon 2026-10-19 13:00 CEST" in lines


class FeedHandler(BaseHTTPRequestHandler):
    """Serves the fixture feed at /ok.ics and a huge response at /huge.ics."""

    def do_GET(self) -> None:
        if self.path == "/ok.ics":
            body = (FIXTURES / "busy.ics").read_bytes()
        elif self.path == "/huge.ics":
            body = b"x" * (availability.MAX_FEED_BYTES + 10)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Keep the test output quiet."""


@pytest.fixture(name="server")
def fixture_server():
    httpd = HTTPServer(("127.0.0.1", 0), FeedHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_real_download(server, monkeypatch):
    monkeypatch.undo()  # use the real _download, not the one of the autouse fixture
    data = availability._download(f"{server}/ok.ics", 5)
    assert data == (FIXTURES / "busy.ics").read_bytes()


def test_download_errors(server, monkeypatch):
    monkeypatch.undo()
    with pytest.raises(OSError):  # 404 is a URLError/HTTPError
        availability._download(f"{server}/missing.ics", 5)
    with pytest.raises(ValueError, match="too large"):
        availability._download(f"{server}/huge.ics", 5)


def test_webcal_is_fetched_over_https(monkeypatch):
    monkeypatch.undo()
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b""

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: seen.append(request.full_url) or Response(),
    )
    availability._download("webcal://cal.example.com/me.ics", 5)
    assert seen == ["https://cal.example.com/me.ics"]
