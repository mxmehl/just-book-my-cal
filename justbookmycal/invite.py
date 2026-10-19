# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Invitations: Jinja2 texts, ICS files and the mails that carry them."""

import logging
import smtplib
import ssl
from collections.abc import Mapping
from datetime import datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
from jinja2.sandbox import SandboxedEnvironment

from . import __version__
from .models import Booking

METHOD_REQUEST = "REQUEST"  # new or changed meeting
METHOD_CANCEL = "CANCEL"

SMTP_TIMEOUT = 15

# Plain-text output, so no HTML autoescaping. The templates come from the owner's config.
_env = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)


def render(template: str, **context: Any) -> str:  # noqa: ANN401
    """Render a Jinja2 template string from the config."""
    return _env.from_string(template).render(**context)


def meeting_url(cfg: Mapping[str, Any], booking: Booking) -> str:
    """URL of the booking's video room."""
    return cfg["MEETING_BASE_URL"].rstrip("/") + "/" + booking.room


def _context(cfg: Mapping[str, Any], booking: Booking, method: str) -> dict[str, Any]:
    """Variables available in all templates."""
    if method == METHOD_CANCEL:
        status = "cancelled"
    else:
        status = "booked" if booking.sequence == 0 else "rescheduled"
    return {
        "booker_name": booking.name,
        "booker_email": booking.email,
        "booker_message": booking.message,
        "owner_name": cfg["OWNER_NAME"],
        "owner_email": cfg["OWNER_EMAIL"],
        "duration": booking.duration,
        "meeting_url": meeting_url(cfg, booking),
        "start_local": booking.start.astimezone(ZoneInfo(cfg["TIMEZONE"])).strftime(
            "%A, %d %B %Y, %H:%M %Z"
        ),
        "start_utc": booking.start.strftime("%Y-%m-%d %H:%M UTC"),
        "status": status,
    }


def _one_line(text: str) -> str:
    """Collapse whitespace, so that a template cannot break a header or a SUMMARY."""
    return " ".join(text.split())


def build_ics(cfg: Mapping[str, Any], booking: Booking, method: str, now: datetime) -> bytes:
    """Create the iCalendar file for a booking.

    The system address is the ORGANIZER, so that Outlook treats the invitation as a normal one for
    both attendees. Updates and cancellations keep the UID and carry a higher SEQUENCE.

    Args:
        cfg: app config
        booking: the booking (its `sequence` must already be the one to send)
        method: METHOD_REQUEST or METHOD_CANCEL
        now: time stamp for DTSTAMP (aware)
    """
    context = _context(cfg, booking, method)
    calendar = icalendar.Calendar()
    calendar.add("prodid", f"-//just-book-my-cal {__version__}//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", method)

    event = icalendar.Event()
    event.add("uid", booking.uid)
    event.add("dtstamp", now)
    event.add("dtstart", booking.start)
    event.add("dtend", booking.end)
    event.add("sequence", booking.sequence)
    event.add("summary", _one_line(render(cfg["EVENT_TITLE"], **context)))
    event.add("description", render(cfg["EVENT_DESCRIPTION"], **context).strip())
    event.add("location", context["meeting_url"])
    event.add("url", context["meeting_url"])
    event.add("status", "CANCELLED" if method == METHOD_CANCEL else "CONFIRMED")
    event.add("transp", "OPAQUE")

    organizer = icalendar.vCalAddress(f"mailto:{cfg['SYSTEM_EMAIL']}")
    organizer.params["CN"] = cfg["OWNER_NAME"]
    event.add("organizer", organizer, encode=False)
    for name, email in ((cfg["OWNER_NAME"], cfg["OWNER_EMAIL"]), (booking.name, booking.email)):
        attendee = icalendar.vCalAddress(f"mailto:{email}")
        attendee.params.update(
            {"CN": name, "ROLE": "REQ-PARTICIPANT", "PARTSTAT": "NEEDS-ACTION", "RSVP": "TRUE"}
        )
        event.add("attendee", attendee, encode=False)

    reminder = cfg["REMINDER_MINUTES"]
    if reminder is not None and method != METHOD_CANCEL:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", "Reminder")
        alarm.add("trigger", timedelta(minutes=-reminder))
        event.add_component(alarm)

    calendar.add_component(event)
    return calendar.to_ical()


def _message(  # noqa: PLR0913
    cfg: Mapping[str, Any],
    booking: Booking,
    method: str,
    ics: bytes,
    recipient: tuple[str, str],
    body_template: str,
    links: dict[str, str | None],
) -> EmailMessage:
    """Build one mail: text/plain + text/calendar alternative, plus the ICS as attachment."""
    context = _context(cfg, booking, method)
    title = _one_line(render(cfg["EVENT_TITLE"], **context))
    prefix = {
        METHOD_CANCEL: "Cancelled",
        METHOD_REQUEST: "Invitation" if booking.sequence == 0 else "Updated invitation",
    }[method]
    system_domain = cfg["SYSTEM_EMAIL"].rpartition("@")[2]

    msg = EmailMessage()
    msg["From"] = Address(cfg["OWNER_NAME"], addr_spec=cfg["SYSTEM_EMAIL"])
    msg["To"] = Address(recipient[0], addr_spec=recipient[1])
    msg["Subject"] = f"{prefix}: {title}"
    msg["Date"] = formatdate()
    msg["Message-ID"] = make_msgid(domain=system_domain)
    msg.set_content(render(body_template, **context, **links))
    msg.add_alternative(ics.decode(), subtype="calendar", params={"method": method})
    msg.add_attachment(ics, maintype="application", subtype="ics", filename="invite.ics")
    return msg


def send_invites(  # noqa: PLR0913
    cfg: Mapping[str, Any],
    booking: Booking,
    method: str,
    now: datetime,
    manage_url: str | None = None,
    admin_url: str | None = None,
) -> bool:
    """Send the invitation (or update, or cancellation) to the booker and the owner.

    Each side only gets its own private link, never the other side's.

    Args:
        cfg: app config
        booking: the booking
        method: METHOD_REQUEST or METHOD_CANCEL
        now: current time (aware)
        manage_url: private link of the booker, goes into the booker's mail
        admin_url: private link of the owner, goes into the owner's mail

    Returns:
        True if both mails were handed to the SMTP server. Failures are logged, never raised, so
        that a mail problem does not lose a booking.
    """
    try:
        ics = build_ics(cfg, booking, method, now)
        messages = [
            _message(
                cfg,
                booking,
                method,
                ics,
                (booking.name, booking.email),
                cfg["EMAIL_BODY_BOOKER"],
                {"manage_url": manage_url, "admin_url": None},
            ),
            _message(
                cfg,
                booking,
                method,
                ics,
                (cfg["OWNER_NAME"], cfg["OWNER_EMAIL"]),
                cfg["EMAIL_BODY_OWNER"],
                {"manage_url": None, "admin_url": admin_url},
            ),
        ]
        return _send(cfg, messages)
    except Exception:  # templates are user-written: nothing may lose a booking
        logging.exception("Could not send invitations for booking %s", booking.uid)
        return False


def _send(cfg: Mapping[str, Any], messages: list[EmailMessage]) -> bool:
    """Send mails over one SMTP connection. A refused recipient does not stop the others."""
    if not cfg["SMTP_HOST"]:
        logging.error("SMTP_HOST is not configured, no mail was sent")
        return False
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=SMTP_TIMEOUT) as smtp:
        if cfg["SMTP_STARTTLS"]:
            smtp.starttls(context=ssl.create_default_context())
        if cfg["SMTP_USER"]:
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        results = [_send_one(smtp, msg) for msg in messages]  # all of them, no short-circuit
    return all(results)


def _send_one(smtp: smtplib.SMTP, msg: EmailMessage) -> bool:
    """Send one mail. A refused recipient is logged and does not stop the other mail."""
    try:
        smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused:
        logging.exception("Recipient refused: %s", msg["To"])
        return False
    return True
