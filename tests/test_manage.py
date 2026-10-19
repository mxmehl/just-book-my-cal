# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the booker's page (/manage/<token>) and the owner's page (/admin/<token>)."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import icalendar
import pytest
from bs4 import BeautifulSoup
from flask.testing import FlaskClient

from justbookmycal import availability
from justbookmycal.extensions import db
from justbookmycal.models import Booking

from .conftest import FIXTURES, NOW, Feed
from .test_views import book, bookings, links, slot_at, slots_of

UTC = timezone.utc


class Booked:
    """A freshly made booking with the private links of both sides."""

    def __init__(self, client: FlaskClient, outbox: list[EmailMessage]) -> None:
        self.slot = slot_at(19, 7, 0)  # Monday 09:00 Berlin, 30 minutes
        response = book(client, self.slot)
        assert response.status_code == 302
        client.get(response.headers["Location"])  # shows (and so clears) the "Done" message
        found = links(outbox)
        self.manage, self.admin = found["manage"], found["admin"]
        outbox.clear()


@pytest.fixture(name="booked")
def fixture_booked(client, outbox) -> Booked:
    return Booked(client, outbox)


def only_booking() -> Booking:
    [booking] = bookings()
    return booking


def calendars(outbox: list[EmailMessage]) -> list[icalendar.Event]:
    """The event in the inline calendar part of every mail."""
    result = []
    for msg in outbox:
        part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
        result.append(
            (part.get_param("method"), icalendar.Calendar.from_ical(part.get_payload(decode=True)))
        )
    return result


def flashes(response) -> list[str]:
    """Messages shown at the top of a page."""
    return [p.text for p in BeautifulSoup(response.data, "html.parser").select(".flash")]


# ---------------------------------------------------------------------------------------------
# Booker
# ---------------------------------------------------------------------------------------------


def test_manage_page_shows_the_meeting_and_other_free_slots(client, booked):
    response = client.get(booked.manage)
    assert response.status_code == 200
    assert "Your meeting with Max Mehl" in response.text
    assert "30 minutes" in response.text
    assert "https://meet.jit.si/MaxMehl-JaneDoe-20261019-" in response.text
    offered = slots_of(response)
    assert offered
    # The meeting's own time can be picked again, because it does not block itself
    assert booked.slot in offered
    # Slots are for the same length: a 30-minute slot that overlaps with nothing else
    assert slot_at(19, 7, 15) in offered


def test_manage_page_is_not_cached_and_does_not_leak_the_url(client, booked):
    response = client.get(booked.manage)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_invalid_token_is_404(client, booked):
    assert client.get("/manage/not-a-token").status_code == 404
    assert (
        client.post("/manage/not-a-token", data={"action": "cancel", "confirm": "1"}).status_code
        == 404
    )
    assert client.get("/manage/not-a-token/invite.ics").status_code == 404


def test_tokens_only_work_on_their_own_side(client, booked):
    admin_token = booked.admin.rsplit("/", 1)[1]
    manage_token = booked.manage.rsplit("/", 1)[1]
    assert client.get(f"/manage/{admin_token}").status_code == 404
    assert client.get(f"/admin/{manage_token}").status_code == 404
    assert (
        client.post(f"/admin/{manage_token}", data={"action": "cancel", "confirm": "1"}).status_code
        == 404
    )
    assert only_booking().status == "confirmed"


def test_ics_download(client, booked):
    response = client.get(booked.manage + "/invite.ics")
    assert response.status_code == 200
    assert response.mimetype == "text/calendar"
    # The file is named after the owner and the meeting time (owner's time zone: 09:00 CEST)
    assert (
        'filename="Meeting-with-MaxMehl-2026-10-19-0900.ics"'
        in response.headers["Content-Disposition"]
    )
    calendar = icalendar.Calendar.from_ical(response.data)
    assert calendar["METHOD"] == "REQUEST"
    assert calendar.walk("VEVENT")[0]["UID"] == only_booking().uid


def test_booker_reschedules_into_a_free_slot(client, booked, outbox):
    before = only_booking()
    new_slot = slot_at(20, 8, 0)  # Tuesday 10:00 Berlin
    response = client.post(booked.manage, data={"action": "reschedule", "slot": new_slot})

    assert response.status_code == 302
    after = only_booking()
    assert after.start == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert after.duration == before.duration
    assert after.sequence == 1
    assert after.uid == before.uid
    assert after.room == before.room  # the video room stays valid in both calendars

    # Both get an update of the same event
    assert len(outbox) == 2
    for method, calendar in calendars(outbox):
        event = calendar.walk("VEVENT")[0]
        assert (method, calendar["METHOD"]) == ("REQUEST", "REQUEST")
        assert event["UID"] == before.uid
        assert event["SEQUENCE"] == 1
        assert event.start == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert all(m["Subject"].startswith("Updated invitation: ") for m in outbox)
    # Both mails carry their own link, although only the booker acted
    assert links(outbox) == {"manage": booked.manage, "admin": booked.admin}
    # The old time is free again, the new one is taken
    offered = slots_of(client.get("/?duration=30"))
    assert booked.slot in offered
    assert new_slot not in offered


def test_booker_cannot_move_into_a_busy_slot(client, booked, outbox, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    availability._cache.clear()
    response = client.post(
        booked.manage, data={"action": "reschedule", "slot": slot_at(22, 8, 0)}
    )  # all-day event
    assert response.status_code == 302
    assert "not available" in flashes(client.get(response.headers["Location"]))[0]
    assert only_booking().start == datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
    assert only_booking().sequence == 0
    assert outbox == []


@pytest.mark.parametrize(
    "slot",
    [slot_at(24, 8, 0), slot_at(20, 3, 0), "garbage", ""],  # Saturday, before working hours, junk
)
def test_booker_cannot_move_outside_the_rules(client, booked, outbox, slot):
    client.post(booked.manage, data={"action": "reschedule", "slot": slot})
    assert only_booking().start == datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
    assert outbox == []


def test_booker_cannot_take_a_slot_booked_by_someone_else(client, booked, outbox):
    assert book(client, slot_at(20, 8, 0), email="other@example.com").status_code == 302
    outbox.clear()
    client.post(booked.manage, data={"action": "reschedule", "slot": slot_at(20, 8, 0)})
    assert only_booking_by_email("jane@example.com").start == datetime(
        2026, 10, 19, 7, 0, tzinfo=UTC
    )


def only_booking_by_email(email: str) -> Booking:
    [booking] = [b for b in bookings() if b.email == email]
    return booking


def test_booker_cancels(client, booked, outbox):
    response = client.post(booked.manage, data={"action": "cancel", "confirm": "on"})
    assert response.status_code == 302
    booking = only_booking()
    assert booking.status == "cancelled"
    assert booking.sequence == 1  # a cancellation must be newer than the invitation

    assert len(outbox) == 2
    for method, calendar in calendars(outbox):
        event = calendar.walk("VEVENT")[0]
        assert (method, calendar["METHOD"], event["STATUS"]) == ("CANCEL", "CANCEL", "CANCELLED")
        assert event["UID"] == booking.uid
        assert event["SEQUENCE"] == 1
    assert all(m["Subject"].startswith("Cancelled: ") for m in outbox)

    # The slot is free again
    assert booked.slot in slots_of(client.get("/?duration=30"))
    # The page says so and offers nothing to change; the calendar file is gone
    page = client.get(booked.manage)
    assert "This meeting was cancelled." in page.text
    assert slots_of(page) == []
    assert client.get(booked.manage + "/invite.ics").status_code == 404


def test_cancelling_needs_the_confirmation(client, booked, outbox):
    response = client.post(booked.manage, data={"action": "cancel"})
    assert "tick the box" in flashes(client.get(response.headers["Location"]))[0]
    assert only_booking().status == "confirmed"
    assert outbox == []


def test_cancelled_booking_cannot_be_changed_or_cancelled_again(client, booked, outbox):
    client.get(
        client.post(booked.manage, data={"action": "cancel", "confirm": "on"}).headers["Location"]
    )
    outbox.clear()
    for data in (
        {"action": "cancel", "confirm": "on"},
        {"action": "reschedule", "slot": slot_at(20, 8, 0)},
    ):
        response = client.post(booked.manage, data=data)
        assert "can no longer be changed" in flashes(client.get(response.headers["Location"]))[0]
    assert only_booking().sequence == 1
    assert outbox == []


def test_booking_in_the_past_cannot_be_changed(client, booked, outbox, monkeypatch):
    monkeypatch.setattr(availability, "utcnow", lambda: NOW + timedelta(days=2))
    page = client.get(booked.manage)
    assert "This meeting is over" in page.text
    response = client.post(booked.manage, data={"action": "cancel", "confirm": "on"})
    assert "can no longer be changed" in flashes(client.get(response.headers["Location"]))[0]
    assert only_booking().status == "confirmed"


def test_unknown_action_is_rejected(client, booked):
    assert client.post(booked.manage, data={"action": "delete"}).status_code == 400


def test_manage_page_still_works_when_the_calendar_is_down(client, booked, feed: Feed):
    from urllib.error import URLError

    availability._cache.clear()
    feed.error = URLError("down")
    page = client.get(booked.manage)
    assert page.status_code == 200
    assert "Moving is not possible right now" in page.text
    assert "Cancel meeting" in page.text
    # Cancelling does not need the calendar
    client.post(booked.manage, data={"action": "cancel", "confirm": "on"})
    assert only_booking().status == "cancelled"


def test_moving_is_refused_when_the_calendar_is_down(client, booked, outbox, feed: Feed):
    from urllib.error import URLError

    availability._cache.clear()
    feed.error = URLError("down")
    response = client.post(booked.manage, data={"action": "reschedule", "slot": slot_at(20, 8, 0)})
    assert response.status_code == 503
    assert only_booking().sequence == 0


# ---------------------------------------------------------------------------------------------
# Owner
# ---------------------------------------------------------------------------------------------


def test_admin_page_shows_booker_and_message(client, booked):
    page = client.get(booked.admin)
    soup = BeautifulSoup(page.data, "html.parser")
    assert (
        "Jane Doe &lt;jane@example.com&gt;" in page.text.replace("<", "&lt;").replace(">", "&gt;")
        or "jane@example.com" in page.text
    )
    assert "Let's talk about the project" in soup.select_one(".message").text
    # Prefilled with the current time in the owner's zone
    assert soup.select_one("#date")["value"] == "2026-10-19"
    assert soup.select_one("#time")["value"] == "09:00"
    assert soup.select_one("#duration")["value"] == "30"


def admin_reschedule(client, booked, **data):
    return client.post(
        booked.admin,
        data={
            "action": "reschedule",
            "date": "2026-10-24",
            "time": "19:00",
            "duration": "45",
            **data,
        },
    )


def test_owner_can_reschedule_outside_the_rules(client, booked, outbox):
    response = admin_reschedule(client, booked)  # Saturday evening, 45 minutes
    assert response.status_code == 302
    booking = only_booking()
    assert booking.start == datetime(2026, 10, 24, 17, 0, tzinfo=UTC)  # 19:00 CEST
    assert booking.duration == 45
    assert booking.sequence == 1
    for method, calendar in calendars(outbox):
        event = calendar.walk("VEVENT")[0]
        assert method == "REQUEST"
        assert event["SEQUENCE"] == 1
        assert event.end - event.start == timedelta(minutes=45)
    assert len(outbox) == 2
    # Both mails carry their own link, although only the owner acted
    assert links(outbox) == {"manage": booked.manage, "admin": booked.admin}


def test_times_are_taken_in_the_owners_time_zone(client, booked):
    admin_reschedule(client, booked, date="2026-10-27", time="09:00")  # after the clocks went back
    assert only_booking().start == datetime(2026, 10, 27, 8, 0, tzinfo=UTC)  # CET = UTC+1


def test_conflict_needs_an_explicit_confirmation(client, booked, outbox, feed: Feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    availability._cache.clear()
    # The moved weekly sync on Monday 2026-10-19 runs 14:00-15:00 Berlin
    response = admin_reschedule(client, booked, date="2026-10-19", time="14:30", duration="30")

    assert response.status_code == 409
    soup = BeautifulSoup(response.data, "html.parser")
    assert "Mon 19 Oct, 14:00-15:00" in soup.select_one(".warning").text
    assert soup.select_one(".warning input[name=confirm]")
    assert soup.select_one("#time")["value"] == "14:30"  # what the owner typed is kept
    assert only_booking().sequence == 0
    assert outbox == []

    response = admin_reschedule(
        client, booked, date="2026-10-19", time="14:30", duration="30", confirm="on"
    )
    assert response.status_code == 302
    assert only_booking().start == datetime(2026, 10, 19, 12, 30, tzinfo=UTC)


def test_overlap_with_another_booking_is_a_conflict(client, booked, outbox):
    assert book(client, slot_at(20, 8, 0), email="other@example.com").status_code == 302
    response = admin_reschedule(client, booked, date="2026-10-20", time="10:15", duration="30")
    assert response.status_code == 409
    assert "Tue 20 Oct, 10:00-10:30" in response.text


def test_moving_within_its_own_time_is_no_conflict(client, booked):
    response = admin_reschedule(client, booked, date="2026-10-19", time="09:15", duration="30")
    assert response.status_code == 302  # overlaps only with itself


@pytest.mark.parametrize(
    "changes",
    [
        {"date": "2026-10-18"},  # in the past (now is 2026-10-19 08:00 Berlin)
        {"date": "2026-02-30"},
        {"date": ""},
        {"time": "25:00"},
        {"time": ""},
        {"duration": "4"},
        {"duration": "481"},
        {"duration": "abc"},
    ],
)
def test_invalid_admin_input_is_rejected(client, booked, outbox, changes):
    response = admin_reschedule(client, booked, **changes)
    assert response.status_code == 400
    assert only_booking().sequence == 0
    assert outbox == []


def test_owner_cancels(client, booked, outbox):
    assert (
        "tick the box"
        in flashes(
            client.get(client.post(booked.admin, data={"action": "cancel"}).headers["Location"])
        )[0]
    )
    assert only_booking().status == "confirmed"

    client.post(booked.admin, data={"action": "cancel", "confirm": "on"})
    assert only_booking().status == "cancelled"
    assert [method for method, _ in calendars(outbox)] == ["CANCEL", "CANCEL"]
    assert links(outbox) == {"manage": booked.manage, "admin": booked.admin}
    assert "This meeting was cancelled." in client.get(booked.admin).text
    assert booked.slot in slots_of(client.get("/?duration=30"))
    # And the booker's link shows the same
    assert "This meeting was cancelled." in client.get(booked.manage).text


def test_admin_cannot_change_a_cancelled_meeting(client, booked, outbox):
    client.get(
        client.post(booked.manage, data={"action": "cancel", "confirm": "on"}).headers["Location"]
    )
    outbox.clear()
    response = admin_reschedule(client, booked)
    assert "can no longer be changed" in flashes(client.get(response.headers["Location"]))[0]
    assert outbox == []


def test_rescheduling_survives_a_mail_failure(client, booked, monkeypatch):
    from justbookmycal import invite

    def broken(cfg, messages):
        raise OSError

    monkeypatch.setattr(invite, "_send", broken)
    response = admin_reschedule(client, booked)
    assert response.status_code == 302
    assert only_booking().sequence == 1
    assert "could not be sent" in flashes(client.get(response.headers["Location"]))[0]


def test_session_lock_is_released_between_requests(client, booked):
    """A request must not leave a transaction open: otherwise the next one would fail."""
    for _ in range(3):
        assert client.get(booked.manage).status_code == 200
        assert client.get(booked.admin).status_code == 200
    db.session.remove()


# ---------------------------------------------------------------------------------------------
# Regression: update and cancellation mails carry the recipient's private link, whoever acted
# ---------------------------------------------------------------------------------------------


def body_of(mail: EmailMessage) -> str:
    """Plain text body of a mail."""
    return mail.get_body(("plain",)).get_content()


def assert_each_side_has_exactly_its_own_link(outbox: list[EmailMessage], booked: Booked) -> None:
    """Booker's mail: booker's link only. Owner's mail: admin link only."""
    booker_mail, owner_mail = outbox
    assert "jane@example.com" in booker_mail["To"].addresses[0].addr_spec
    assert "If you need to change the date or cancel" in body_of(booker_mail)
    assert booked.manage in body_of(booker_mail)
    assert "/admin/" not in body_of(booker_mail)
    assert "Reschedule freely or cancel with your private admin link" in body_of(owner_mail)
    assert booked.admin in body_of(owner_mail)
    assert "/manage/" not in body_of(owner_mail)


def test_update_by_booker_still_gives_the_owner_the_admin_link(client, booked, outbox):
    client.post(booked.manage, data={"action": "reschedule", "slot": slot_at(20, 8, 0)})
    assert_each_side_has_exactly_its_own_link(outbox, booked)


def test_update_by_owner_still_gives_the_booker_the_manage_link(client, booked, outbox):
    admin_reschedule(client, booked)
    assert_each_side_has_exactly_its_own_link(outbox, booked)


def test_cancellation_by_booker_gives_both_their_links(client, booked, outbox):
    client.post(booked.manage, data={"action": "cancel", "confirm": "on"})
    assert_each_side_has_exactly_its_own_link(outbox, booked)


def test_links_stay_the_same_over_several_changes(client, booked, outbox):
    client.post(booked.manage, data={"action": "reschedule", "slot": slot_at(20, 8, 0)})
    outbox.clear()
    admin_reschedule(client, booked, date="2026-10-21")
    assert_each_side_has_exactly_its_own_link(outbox, booked)
    # The original links still work after all changes
    assert client.get(booked.manage).status_code == 200
    assert client.get(booked.admin).status_code == 200


def test_manage_page_lists_one_week_with_arrows(client, booked):
    page = client.get(booked.manage)
    soup = BeautifulSoup(page.data, "html.parser")
    nav = soup.select_one("nav[aria-label=Week]")
    assert nav.select_one("strong").text == "Mon 19 Oct – Sun 25 Oct 2026"
    assert nav.select_one("a[rel=prev]") is None
    next_url = nav.select_one("a[rel=next]")["href"]
    assert next_url.endswith(f"{booked.manage}?week=2026-10-26")
    assert max(s[:10] for s in slots_of(page)) <= "2026-10-25"

    later = client.get(next_url)
    assert slots_of(later)
    assert min(s[:10] for s in slots_of(later)) >= "2026-10-26"


def test_booker_can_move_into_a_later_week(client, booked, outbox):
    later = slots_of(client.get(booked.manage + "?week=2026-10-26"))[0]
    client.post(booked.manage, data={"action": "reschedule", "slot": later})
    assert only_booking().start.strftime("%Y-%m-%dT%H:%M:%SZ") == later
