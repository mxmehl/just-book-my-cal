# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the invitation: ICS content, MIME structure, templates and SMTP."""

import smtplib
from datetime import datetime, timezone

import icalendar
import pytest

from justbookmycal import invite
from justbookmycal.models import Booking

from .conftest import NOW, TEST_CONFIG

UTC = timezone.utc

CFG = {
    **TEST_CONFIG,
    "MEETING_BASE_URL": "https://meet.example.com/",
    "REMINDER_MINUTES": 15,
    "EVENT_TITLE": "{{ booker_name }} / {{ owner_name }} ({{ duration }} min)",
    "EVENT_DESCRIPTION": (
        "{% if booker_message %}{{ booker_message }}\n\n{% endif %}Join: {{ meeting_url }}"
    ),
    "EMAIL_BODY_BOOKER": (
        "Hi {{ booker_name }}, {{ start_local }} / {{ start_utc }} ({{ status }})"
        "{% if manage_url %} {{ manage_url }}{% endif %}"
    ),
    "EMAIL_BODY_OWNER": (
        "{{ booker_email }} {{ status }}{% if admin_url %} {{ admin_url }}{% endif %}"
    ),
    "SMTP_PORT": 587,
    "SMTP_USER": "",
    "SMTP_PASS": "",
    "SMTP_STARTTLS": True,
}


def make_booking(**changes) -> Booking:
    """A booking at Tuesday 2026-10-20 10:00 Berlin (08:00 UTC)."""
    values = {
        "uid": "abc@example.com",
        "start": datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        "duration": 30,
        "name": "Jane Doe",
        "email": "jane@example.com",
        "message": "Let's talk about the project",
        "room": "MaxMehl-JaneDoe-20261020-k7p2x",
        "booker_token_hash": "x",
        "admin_token_hash": "y",
    }
    return Booking(**{**values, **changes})


def event_of(ics: bytes) -> icalendar.Event:
    """Parse an ICS file and return its only event."""
    calendar = icalendar.Calendar.from_ical(ics)
    [event] = calendar.walk("VEVENT")
    return event


# ---------------------------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------------------------


def test_ics_of_a_new_booking():
    ics = invite.build_ics(CFG, make_booking(), invite.METHOD_REQUEST, NOW)
    calendar = icalendar.Calendar.from_ical(ics)
    event = event_of(ics)

    assert calendar["METHOD"] == "REQUEST"
    assert calendar["VERSION"] == "2.0"
    assert event["UID"] == "abc@example.com"
    assert event["SEQUENCE"] == 0
    assert event["STATUS"] == "CONFIRMED"
    assert event["SUMMARY"] == "Jane Doe / Max Mehl (30 min)"
    assert (
        event["DESCRIPTION"]
        == "Let's talk about the project\n\nJoin: https://meet.example.com/MaxMehl-JaneDoe-20261020-k7p2x"
    )
    assert event["LOCATION"] == "https://meet.example.com/MaxMehl-JaneDoe-20261020-k7p2x"
    assert event.start == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 10, 20, 8, 30, tzinfo=UTC)
    # Times are written in UTC with a "Z", so no VTIMEZONE is needed
    assert b"DTSTART:20261020T080000Z" in ics
    assert b"VTIMEZONE" not in ics


def test_system_address_is_the_organizer_and_both_are_attendees():
    event = event_of(invite.build_ics(CFG, make_booking(), invite.METHOD_REQUEST, NOW))
    assert str(event["ORGANIZER"]) == "mailto:noreply@example.com"
    assert event["ORGANIZER"].params["CN"] == "Max Mehl"
    attendees = {str(a): a.params for a in event["ATTENDEE"]}
    assert set(attendees) == {"mailto:max@example.com", "mailto:jane@example.com"}
    assert attendees["mailto:jane@example.com"]["CN"] == "Jane Doe"
    assert all(p["PARTSTAT"] == "NEEDS-ACTION" and p["RSVP"] == "TRUE" for p in attendees.values())


def test_reminder_becomes_a_valarm():
    event = event_of(invite.build_ics(CFG, make_booking(), invite.METHOD_REQUEST, NOW))
    [alarm] = event.walk("VALARM")
    assert alarm["ACTION"] == "DISPLAY"
    assert alarm["TRIGGER"].dt.total_seconds() == -15 * 60


def test_no_reminder_means_no_valarm():
    ics = invite.build_ics(
        {**CFG, "REMINDER_MINUTES": None}, make_booking(), invite.METHOD_REQUEST, NOW
    )
    assert b"VALARM" not in ics


def test_update_keeps_the_uid_and_raises_the_sequence():
    first = event_of(invite.build_ics(CFG, make_booking(), invite.METHOD_REQUEST, NOW))
    moved = event_of(
        invite.build_ics(
            CFG,
            make_booking(sequence=1, start=datetime(2026, 10, 21, 9, 0, tzinfo=UTC)),
            invite.METHOD_REQUEST,
            NOW,
        )
    )
    assert moved["UID"] == first["UID"]
    assert moved["SEQUENCE"] == 1
    assert moved.start != first.start


def test_cancellation():
    ics = invite.build_ics(CFG, make_booking(sequence=2), invite.METHOD_CANCEL, NOW)
    event = event_of(ics)
    assert icalendar.Calendar.from_ical(ics)["METHOD"] == "CANCEL"
    assert event["STATUS"] == "CANCELLED"
    assert event["SEQUENCE"] == 2
    assert event["UID"] == "abc@example.com"
    assert b"VALARM" not in ics


def test_empty_message_leaves_no_blank_block():
    cfg = CFG
    event = event_of(invite.build_ics(cfg, make_booking(message=""), invite.METHOD_REQUEST, NOW))
    assert event["DESCRIPTION"] == "Join: https://meet.example.com/MaxMehl-JaneDoe-20261020-k7p2x"


def test_special_characters_survive_the_round_trip():
    booking = make_booking(name="Zoë, O'Brien; Jr.", message="Line 1\nLine 2, with; special: chars")
    event = event_of(invite.build_ics(CFG, booking, invite.METHOD_REQUEST, NOW))
    assert event["SUMMARY"] == "Zoë, O'Brien; Jr. / Max Mehl (30 min)"
    assert "Line 1\nLine 2, with; special: chars" in event["DESCRIPTION"]
    by_name = {a.params["CN"]: a for a in event["ATTENDEE"]}
    assert by_name["Zoë, O'Brien; Jr."].params["CN"] == "Zoë, O'Brien; Jr."
    assert str(by_name["Zoë, O'Brien; Jr."]) == "mailto:jane@example.com"


def test_template_cannot_break_the_title_line():
    cfg = {**CFG, "EVENT_TITLE": "A\r\nBCC: evil@example.com\n{{ booker_name }}"}
    event = event_of(invite.build_ics(cfg, make_booking(), invite.METHOD_REQUEST, NOW))
    assert "\n" not in event["SUMMARY"]
    assert "ATTENDEE" in event  # still intact


def test_templates_are_sandboxed():
    with pytest.raises(Exception, match=r"unsafe|SecurityError|not safe"):
        invite.render("{{ ''.__class__.__mro__[1].__subclasses__() }}")


# ---------------------------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------------------------


def build(booking: Booking, method: str = invite.METHOD_REQUEST, **links):
    """Build the booker's and the owner's mail the way send_invites does and return both."""
    sent = []
    original = invite._send
    invite._send = lambda cfg, messages: sent.extend(messages) or True  # type: ignore[assignment]
    try:
        assert invite.send_invites(CFG, booking, method, NOW, **links)
    finally:
        invite._send = original
    return sent


def test_mime_structure():
    booker_mail, _owner_mail = build(make_booking(), manage_url="https://x/manage/t")
    assert booker_mail.get_content_type() == "multipart/mixed"
    alternative, attachment = booker_mail.get_payload()
    assert alternative.get_content_type() == "multipart/alternative"
    plain, calendar = alternative.get_payload()
    assert plain.get_content_type() == "text/plain"
    assert calendar.get_content_type() == "text/calendar"
    assert calendar.get_param("method") == "REQUEST"
    assert attachment.get_content_type() == "application/ics"
    assert attachment.get_filename() == "invite.ics"
    # Both calendar parts carry the same invitation (line endings differ by transfer encoding)
    inline = calendar.get_payload(decode=True)
    assert attachment.get_payload(decode=True).replace(b"\r\n", b"\n") == inline.replace(
        b"\r\n", b"\n"
    )


def test_headers():
    booker_mail, owner_mail = build(make_booking())
    assert str(booker_mail["From"]) == "Max Mehl <noreply@example.com>"
    assert str(booker_mail["To"]) == "Jane Doe <jane@example.com>"
    assert str(owner_mail["To"]) == "Max Mehl <max@example.com>"
    assert booker_mail["Subject"] == "Invitation: Jane Doe / Max Mehl (30 min)"
    assert booker_mail["Message-ID"].endswith("@example.com>")


@pytest.mark.parametrize(
    ("sequence", "method", "subject"),
    [
        (0, invite.METHOD_REQUEST, "Invitation: "),
        (1, invite.METHOD_REQUEST, "Updated invitation: "),
        (2, invite.METHOD_CANCEL, "Cancelled: "),
    ],
)
def test_subjects(sequence, method, subject):
    [mail, _] = build(make_booking(sequence=sequence), method)
    assert mail["Subject"].startswith(subject)


def test_cancellation_mail_carries_a_cancel_method():
    [mail, _] = build(make_booking(sequence=1), invite.METHOD_CANCEL)
    calendar = mail.get_payload()[0].get_payload()[1]
    assert calendar.get_param("method") == "CANCEL"
    assert "(cancelled)" in mail.get_body(("plain",)).get_content()


def test_each_mail_contains_only_its_own_link():
    booker_mail, owner_mail = build(
        make_booking(), manage_url="https://x/manage/t", admin_url="https://x/admin/t"
    )
    booker_text = booker_mail.get_body(("plain",)).get_content()
    owner_text = owner_mail.get_body(("plain",)).get_content()
    assert "https://x/manage/t" in booker_text
    assert "/admin/" not in booker_text
    assert "https://x/admin/t" in owner_text
    assert "/manage/" not in owner_text


def test_links_are_left_out_when_unknown():
    booker_mail, owner_mail = build(make_booking(sequence=1))
    assert "http" not in booker_mail.get_body(("plain",)).get_content()
    assert owner_mail.get_body(("plain",)).get_content().strip() == "jane@example.com rescheduled"


def test_times_in_the_text_name_both_zones():
    [mail, _] = build(make_booking())
    text = mail.get_body(("plain",)).get_content()
    assert "Tuesday, 20 October 2026, 10:00 CEST" in text
    assert "2026-10-20 08:00 UTC" in text


def test_non_ascii_names_in_headers():
    [mail, _] = build(make_booking(name="Zoë Müller"))
    assert mail["To"].addresses[0].display_name == "Zoë Müller"
    mail.as_bytes()  # serialises without errors


# ---------------------------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------------------------


class FakeSMTP:
    """Records what would happen on an SMTP connection."""

    instances: list["FakeSMTP"] = []  # noqa: RUF012
    refuse: set[str] = set()  # noqa: RUF012

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        self.sent: list[str] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args) -> None:
        self.calls.append(("quit",))

    def starttls(self, context) -> None:
        self.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))

    def send_message(self, msg) -> None:
        if str(msg["To"].addresses[0].addr_spec) in FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({})
        self.sent.append(str(msg["To"]))


@pytest.fixture(name="smtp")
def fixture_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.refuse = set()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_mails_go_out_over_one_starttls_connection(smtp):
    cfg = {**CFG, "SMTP_USER": "user", "SMTP_PASS": "secret"}
    assert invite.send_invites(cfg, make_booking(), invite.METHOD_REQUEST, NOW)
    [connection] = smtp.instances
    assert (connection.host, connection.port) == ("smtp.example.com", 587)
    assert connection.calls[:2] == [("starttls",), ("login", "user", "secret")]
    assert connection.sent == ["Jane Doe <jane@example.com>", "Max Mehl <max@example.com>"]


def test_no_login_without_credentials(smtp):
    invite.send_invites(CFG, make_booking(), invite.METHOD_REQUEST, NOW)
    assert ("login", "", "") not in smtp.instances[0].calls
    assert not any(c[0] == "login" for c in smtp.instances[0].calls)


def test_a_refused_recipient_does_not_stop_the_other_mail(smtp):
    smtp.refuse = {"jane@example.com"}
    assert not invite.send_invites(CFG, make_booking(), invite.METHOD_REQUEST, NOW)
    assert smtp.instances[0].sent == ["Max Mehl <max@example.com>"]


def test_unreachable_server_is_reported_not_raised(monkeypatch, caplog):
    def refuse(*args, **kwargs):
        msg = "connection refused"
        raise ConnectionRefusedError(msg)

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    assert not invite.send_invites(CFG, make_booking(), invite.METHOD_REQUEST, NOW)
    assert "Could not send invitations" in caplog.text


def test_missing_smtp_host_is_reported(caplog):
    assert not invite.send_invites(
        {**CFG, "SMTP_HOST": ""}, make_booking(), invite.METHOD_REQUEST, NOW
    )
    assert "SMTP_HOST is not configured" in caplog.text


def test_broken_template_does_not_raise(monkeypatch, caplog):
    cfg = {**CFG, "EMAIL_BODY_BOOKER": "{{ 1 / 0 }}"}
    assert not invite.send_invites(cfg, make_booking(), invite.METHOD_REQUEST, NOW)
