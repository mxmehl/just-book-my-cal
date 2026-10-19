# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the public booking page and the booking POST."""

import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.error import URLError

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from justbookmycal import availability, invite
from justbookmycal.app import create_app
from justbookmycal.extensions import db
from justbookmycal.models import Booking, hash_token

from .conftest import FIXTURES, NOW, TEST_CONFIG, Feed

UTC = timezone.utc


def slots_of(response) -> list[str]:
    """The slot values offered on a page."""
    soup = BeautifulSoup(response.data, "html.parser")
    return [i["value"] for i in soup.select("input[name=slot]")]


def first_slot(client: FlaskClient, duration: int = 30, index: int = 0) -> str:
    """One of the offered slots of a meeting length."""
    return slots_of(client.get(f"/?duration={duration}"))[index]


def slot_at(day: int, hour: int, minute: int = 0) -> str:
    """Slot value for a time in October 2026, given in UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def book(client: FlaskClient, slot: str | None = None, **changes):
    """POST the booking form."""
    data = {
        "slot": first_slot(client) if slot is None else slot,
        "duration": 30,
        "name": "Jane Doe",
        "email": "jane@example.com",
        "message": "Let's talk about the project",
        "website": "",
    }
    return client.post("/book", data={**data, **changes})


def bookings() -> list[Booking]:
    """All bookings, read in a way that does not block later requests."""
    result = list(db.session.scalars(select(Booking).order_by(Booking.id)))
    db.session.remove()
    return result


def links(outbox: list[EmailMessage]) -> dict[str, str]:
    """The private links found in the mails sent so far: {'manage': ..., 'admin': ...}."""
    found = {}
    for msg in outbox:
        text = msg.get_body(("plain",)).get_content()  # type: ignore[union-attr]
        for kind in ("manage", "admin"):
            if match := re.search(rf"http://localhost/{kind}/[\w-]+", text):
                found[kind] = match.group(0).removeprefix("http://localhost")
    return found


# ---------------------------------------------------------------------------------------------
# Booking page
# ---------------------------------------------------------------------------------------------


def test_index_lists_slots_with_utc_times_and_zone_label(client):
    response = client.get("/")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("h1").text == "Book a meeting with Max Mehl"
    times = soup.select("input[name=slot] + label time")
    assert times
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", times[0]["datetime"])
    assert "Europe/Berlin" in soup.select_one("[data-tz-note]").text  # shown without JavaScript
    assert "data-slot-picker" in response.text
    assert soup.select_one("input[name=website]")  # honeypot


def test_index_first_slot_is_the_next_free_time(client):
    # Now is Monday 08:00 in Berlin, no notice, working hours start at 09:00
    assert first_slot(client, 30) == slot_at(19, 7, 0)  # 09:00 CEST


def test_durations_are_offered_as_navigation(client):
    soup = BeautifulSoup(client.get("/?duration=60").data, "html.parser")
    links_ = {a.text: a.get("aria-current") for a in soup.select("nav a")}
    assert links_ == {"15 minutes": None, "30 minutes": None, "60 minutes": "page"}


def test_longer_meetings_have_fewer_slots(client):
    assert len(slots_of(client.get("/?duration=15"))) > len(slots_of(client.get("/?duration=60")))


def test_unknown_duration_is_rejected(client):
    assert client.get("/?duration=45").status_code == 400


def test_busy_times_from_the_feed_are_not_offered(client, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    offered = slots_of(client.get("/?duration=60"))
    assert slot_at(19, 8, 0) in offered  # the weekly sync at 10:00 Berlin was moved away...
    assert slot_at(19, 12, 0) not in offered  # ...to 14:00 Berlin
    assert slot_at(19, 11, 0) in offered  # 13:00-14:00 Berlin ends when the sync starts
    assert slot_at(19, 13, 0) in offered  # 15:00 Berlin starts when it ends
    assert slot_at(22, 8, 0) not in offered  # the all-day event blocks Thursday
    assert slot_at(20, 7, 0) in offered  # transparent event on Tuesday does not block


def test_unreachable_feed_shows_a_friendly_error(client, feed: Feed):
    feed.error = URLError("down")
    response = client.get("/")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    assert "down" not in response.text  # no internals


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------------------------


def test_booking_is_saved_and_both_get_an_invitation(client, outbox):
    slot = first_slot(client)
    response = book(client, slot)

    assert response.status_code == 302
    [booking] = bookings()
    assert booking.start == datetime.fromisoformat(slot.replace("Z", "+00:00"))
    assert (booking.duration, booking.name, booking.email) == (30, "Jane Doe", "jane@example.com")
    assert booking.message == "Let's talk about the project"
    assert (booking.sequence, booking.status) == (0, "confirmed")
    assert booking.uid.endswith("@example.com")
    assert re.fullmatch(r"MaxMehl-JaneDoe-20261019-[a-z2-9]{5}", booking.room)

    assert sorted(str(m["To"]) for m in outbox) == [
        "Jane Doe <jane@example.com>",
        "Max Mehl <max@example.com>",
    ]
    # The redirect leads to the booker's private page, which is the one named in the mail
    assert response.headers["Location"].endswith(links(outbox)["manage"])
    page = client.get(response.headers["Location"])
    assert "Your meeting with Max Mehl" in page.text
    assert "The calendar invitation was sent" in page.text


def test_only_hashes_of_the_tokens_are_stored(client, outbox):
    book(client)
    [booking] = bookings()
    found = links(outbox)
    manage_token, admin_token = (found[k].rsplit("/", 1)[1] for k in ("manage", "admin"))
    assert manage_token != admin_token
    assert booking.booker_token_hash == hash_token(manage_token)
    assert booking.admin_token_hash == hash_token(admin_token)
    assert manage_token not in booking.booker_token_hash


def test_each_side_only_gets_its_own_link(client, outbox):
    book(client)
    booker_mail, owner_mail = outbox
    assert "/manage/" in booker_mail.get_body(("plain",)).get_content()
    assert "/admin/" not in booker_mail.get_body(("plain",)).get_content()
    assert "/admin/" in owner_mail.get_body(("plain",)).get_content()
    assert "/manage/" not in owner_mail.get_body(("plain",)).get_content()


def test_booked_slot_disappears(client, outbox):
    slot = first_slot(client)
    book(client, slot)
    assert slot not in slots_of(client.get("/?duration=30"))
    # Also overlapping slots of other lengths are gone
    assert slot not in slots_of(client.get("/?duration=15"))


def test_slot_cannot_be_booked_twice(client, outbox):
    slot = first_slot(client)
    assert book(client, slot).status_code == 302
    second = book(client, slot, email="other@example.com")
    assert second.status_code == 409
    assert "no longer available" in second.text
    assert len(bookings()) == 1
    assert len(outbox) == 2


def test_overlapping_slot_cannot_be_booked(client, outbox):
    assert book(client, slot_at(19, 7, 0)).status_code == 302  # 09:00-09:30
    assert book(client, slot_at(19, 7, 15), email="other@example.com").status_code == 409


@pytest.mark.parametrize(
    "slot",
    [
        slot_at(24, 8, 0),  # Saturday
        slot_at(19, 3, 0),  # Monday 05:00 Berlin, before working hours
        slot_at(19, 7, 7),  # not on the 15-minute grid
        slot_at(19, 15, 0),  # 17:00 Berlin, after working hours
        slot_at(19, 14, 45),  # 16:45 Berlin: a 30-minute meeting would run past 17:00
        "2026-10-19T07:00:00",  # no time zone
        "garbage",
    ],
)
def test_slots_outside_the_rules_are_rejected(client, outbox, slot):
    assert book(client, slot).status_code == 409
    assert bookings() == []


def test_slot_blocked_by_the_feed_is_rejected(client, outbox, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    assert book(client, slot_at(22, 8, 0)).status_code == 409  # Thursday, all-day event


def test_honeypot_creates_nothing(client, outbox):
    response = book(client, website="http://spam.example.com")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert bookings() == []
    assert outbox == []


def test_email_cap_applies_case_insensitively(client, outbox):
    assert book(client, slot_at(19, 7, 0)).status_code == 302
    assert book(client, slot_at(19, 8, 0), email="Jane@Example.com").status_code == 302
    third = book(client, slot_at(19, 9, 0))
    assert third.status_code == 429
    assert "enough upcoming meetings" in third.text
    assert len(bookings()) == 2
    assert book(client, slot_at(19, 9, 0), email="someone-else@example.com").status_code == 302


def test_past_bookings_do_not_count_for_the_cap(app, client, outbox):
    for i in range(2):
        db.session.add(
            Booking(
                uid=f"old{i}",
                start=NOW - timedelta(days=3 + i),
                duration=30,
                name="Jane",
                email="jane@example.com",
                room="r",
                booker_token_hash=f"a{i}",
                admin_token_hash=f"b{i}",
            )
        )
    db.session.commit()
    db.session.remove()
    assert book(client).status_code == 302


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "not-an-email"},
        {"email": ""},
        {"name": ""},
        {"name": 'Jane "the" Doe'},
        {"name": "Jane\r\nBcc: evil@example.com"},
        {"name": "x" * 101},
        {"message": "x" * 2001},
        {"slot": ""},
    ],
)
def test_invalid_input_is_rejected(client, outbox, changes):
    response = book(client, **changes)
    assert response.status_code == 400
    assert bookings() == []
    assert outbox == []


def test_unknown_duration_in_the_form_is_rejected(client, outbox):
    assert book(client, duration=45).status_code == 400
    assert bookings() == []


def test_mail_failure_keeps_the_booking_and_tells_the_user(client, monkeypatch, caplog):
    def broken(cfg, messages):
        msg = "connection refused"
        raise OSError(msg)

    monkeypatch.setattr(invite, "_send", broken)
    response = book(client)
    assert response.status_code == 302
    assert len(bookings()) == 1
    page = client.get(response.headers["Location"])
    assert "could not be sent" in page.text
    # The owner's admin link is the only way to it now, so it is in the log
    assert "/admin/" in caplog.text


def test_unreachable_feed_refuses_bookings(client, outbox, feed: Feed):
    slot = first_slot(client)
    availability._cache.clear()
    feed.error = URLError("down")
    assert book(client, slot).status_code == 503
    assert bookings() == []


def test_csrf_token_is_required():
    app = create_app(config_overrides={**TEST_CONFIG, "WTF_CSRF_ENABLED": True})
    with app.app_context():
        db.create_all()
    client = app.test_client()
    response = client.post(
        "/book",
        data={"slot": slot_at(19, 7), "duration": 30, "name": "J", "email": "j@example.com"},
    )
    assert response.status_code == 400
    assert "CSRF" in response.text


def test_posts_are_rate_limited():
    app = create_app(
        config_overrides={
            **TEST_CONFIG,
            "RATELIMIT_ENABLED": True,
            "RATE_LIMIT_POST": "2 per minute",
        }
    )
    with app.app_context():
        db.create_all()
    client = app.test_client()
    codes = [client.post("/book", data={}).status_code for _ in range(3)]
    assert codes[-1] == 429
    assert 429 not in codes[:2]
    assert client.get("/").status_code == 200  # reading is not limited


def test_forwarded_client_ip_is_only_trusted_behind_a_proxy():
    app = create_app(
        config_overrides={
            **TEST_CONFIG,
            "RATELIMIT_ENABLED": True,
            "RATE_LIMIT_POST": "1 per minute",
            "TRUSTED_PROXIES": 1,
        }
    )
    with app.app_context():
        db.create_all()
    client = app.test_client()
    first = client.post("/book", data={}, headers={"X-Forwarded-For": "203.0.113.1"})
    other_visitor = client.post("/book", data={}, headers={"X-Forwarded-For": "203.0.113.2"})
    same_visitor = client.post("/book", data={}, headers={"X-Forwarded-For": "203.0.113.1"})
    assert (first.status_code, other_visitor.status_code) == (400, 400)
    assert same_visitor.status_code == 429


def test_unknown_page_gets_the_error_page(client):
    response = client.get("/nothing-here")
    assert response.status_code == 404
    assert "Back to the booking page" in response.text


def test_app_fixture_is_isolated_between_tests(app: Flask):
    assert bookings() == []


def test_derived_tokens_are_stable_distinct_and_depend_on_the_secret():
    from justbookmycal.models import derive_token

    manage = derive_token("s" * 32, "uid-1@example.com", "manage")
    assert manage == derive_token("s" * 32, "uid-1@example.com", "manage")
    assert manage != derive_token("s" * 32, "uid-1@example.com", "admin")
    assert manage != derive_token("s" * 32, "uid-2@example.com", "manage")
    assert manage != derive_token("t" * 32, "uid-1@example.com", "manage")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", manage)  # 256 bits, URL-safe


def test_changing_the_secret_key_breaks_later_links_but_not_sent_ones(app, client, outbox):
    book(client)
    link = links(outbox)["manage"]
    old_secret = app.config["SECRET_KEY"]
    app.config["SECRET_KEY"] = "another-secret-key-0123456789abcdef"
    assert client.get(link).status_code == 200  # looked up by the stored hash
    outbox.clear()
    manage_post = client.post(link, data={"action": "reschedule", "slot": slot_at(20, 8, 0)})
    assert manage_post.status_code == 302
    assert links(outbox)["manage"] != link  # derived from the new secret: does not match anymore
    assert client.get(links(outbox)["manage"]).status_code == 404
    app.config["SECRET_KEY"] = old_secret
