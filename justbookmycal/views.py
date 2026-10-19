# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Routes: the public booking page and the private manage (booker) and admin (owner) pages."""

import logging
import secrets
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_wtf import FlaskForm
from sqlalchemy import func, select
from werkzeug.wrappers import Response as WerkzeugResponse
from wtforms import EmailField, IntegerField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Regexp

from . import availability, invite
from .availability import FeedError
from .extensions import db, limiter
from .models import STATUS_CANCELLED, STATUS_CONFIRMED, Booking, hash_token, new_token

bp = Blueprint("main", __name__)
# Every POST (booking, rescheduling, cancelling) is rate limited per client IP
limiter.limit(lambda: current_app.config["RATE_LIMIT_POST"], methods=["POST"])(bp)

ROOM_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (i, l, o, 0, 1)
NO_CONTROL_CHARS_OR_QUOTES = r'^[^\x00-\x1f\x7f"]+$'  # names end up in mail headers and ICS
MIN_ADMIN_DURATION = 5
MAX_ADMIN_DURATION = 480
SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
Reply = str | tuple[str, int] | WerkzeugResponse


class BookingForm(FlaskForm):
    """The booking form. Also provides the CSRF token."""

    slot = StringField(validators=[DataRequired()])
    duration = IntegerField(validators=[DataRequired()])
    name = StringField(
        "Your name",
        validators=[
            DataRequired(),
            Length(max=100),
            Regexp(NO_CONTROL_CHARS_OR_QUOTES, message="Please use plain characters only."),
        ],
    )
    email = EmailField("Your email address", validators=[DataRequired(), Email(), Length(max=320)])
    message = TextAreaField("What is it about? (optional)", validators=[Length(max=2000)])
    website = StringField("Leave this field empty")  # honeypot: humans never see it


@bp.app_context_processor
def _inject() -> dict[str, str]:
    """Variables for all templates."""
    return {
        "owner_name": current_app.config["OWNER_NAME"],
        "timezone_name": current_app.config["TIMEZONE"],
    }


@bp.before_request
def _prefetch_feeds() -> None:
    """Warm the feed cache before any database access (see availability.prefetch)."""
    try:
        availability.prefetch(current_app.config)
    except FeedError as e:
        # Handled where slots are actually needed, so that e.g. cancelling still works
        logging.warning("%s", e)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _tz() -> ZoneInfo:
    """The owner's time zone."""
    return ZoneInfo(current_app.config["TIMEZONE"])


def _parse_slot(value: str) -> datetime | None:
    """Parse a slot value as posted by the form; None if it is not an aware datetime."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _days(slots: list[datetime]) -> list[dict[str, Any]]:
    """Group slots by day in the owner's time zone, as the template wants them.

    Without JavaScript, this is what the visitor sees. With JavaScript, the times are regrouped in
    the visitor's own time zone (static/slots.js).
    """
    days: dict[date, list[dict[str, str]]] = {}
    for slot in slots:
        local = slot.astimezone(_tz())
        days.setdefault(local.date(), []).append(
            {"iso": slot.strftime(SLOT_FORMAT), "label": f"{local:%H:%M}"}
        )
    return [{"label": f"{day:%A, %d %B %Y}", "slots": s} for day, s in days.items()]


def _valid_duration(value: int | None) -> int:
    """Return the duration if the owner offers it, else answer 400."""
    if value is None or value not in current_app.config["DURATIONS"]:
        abort(400, "This meeting length is not offered.")
    return value


def _make_room(booker_name: str, start: datetime) -> str:
    """Name of the video room: OwnerName-BookerName-YYYYMMDD-random."""

    def ascii_alnum(text: str) -> str:
        ascii_text = unicodedata.normalize("NFKD", text.title()).encode("ascii", "ignore").decode()
        return "".join(c for c in ascii_text if c.isalnum())[:20] or "Guest"

    suffix = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(5))
    day = start.astimezone(_tz())
    owner = ascii_alnum(current_app.config["OWNER_NAME"])
    return f"{owner}-{ascii_alnum(booker_name)}-{day:%Y%m%d}-{suffix}"


def _booking_by_token(column: Any, token: str) -> Booking:  # noqa: ANN401
    """Find a booking by one of its secret tokens, or answer 404."""
    booking = db.session.scalar(select(Booking).where(column == hash_token(token)))
    if booking is None:
        abort(404, "This link is not valid.")
    return booking


def _is_open(booking: Booking, now: datetime) -> bool:
    """Whether a booking can still be changed: not cancelled and not over."""
    return not booking.cancelled and booking.end > now


def _notify(
    booking: Booking,
    method: str,
    now: datetime,
    manage_url: str | None = None,
    admin_url: str | None = None,
) -> None:
    """Send the invitations and tell the user how it went."""
    if invite.send_invites(current_app.config, booking, method, now, manage_url, admin_url):
        flash("Done. The calendar invitation was sent by email to both of you.", "ok")
        return
    # The links exist only in the mails. If those fail, the owner's log is the only place left.
    logging.error("Mails for booking %s failed. Admin link: %s", booking.uid, admin_url)
    flash(
        "The change is saved, but the email with the invitation could not be sent. "
        "You can still download the calendar file below.",
        "error",
    )


def _reschedule(
    booking: Booking, start: datetime, duration: int, now: datetime, **links: str | None
) -> None:
    """Move a booking. Same UID, higher SEQUENCE: calendars update the entry instead of adding."""
    booking.start, booking.duration = start, duration
    booking.sequence += 1
    db.session.commit()
    _notify(booking, invite.METHOD_REQUEST, now, **links)


def _cancel(booking: Booking, now: datetime, **links: str | None) -> None:
    """Cancel a booking; this frees the slot."""
    booking.status = STATUS_CANCELLED
    booking.sequence += 1
    db.session.commit()
    _notify(booking, invite.METHOD_CANCEL, now, **links)


# ---------------------------------------------------------------------------------------------
# Public booking page
# ---------------------------------------------------------------------------------------------


def _render_index(form: BookingForm, duration: int, status: int = 200) -> Reply:
    """Render the booking page with the free slots of a meeting length."""
    cfg = current_app.config
    slots = availability.free_slots(cfg, duration, availability.utcnow())
    db.session.rollback()  # release the database lock before rendering
    return (
        render_template(
            "index.html",
            form=form,
            duration=duration,
            durations=cfg["DURATIONS"],
            days=_days(slots),
            horizon_days=cfg["HORIZON_DAYS"],
        ),
        status,
    )


@bp.get("/")
def index() -> Reply:
    """Duration picker and the free slots."""
    duration = _valid_duration(
        request.args.get("duration", default=current_app.config["DURATIONS"][0], type=int)
    )
    return _render_index(BookingForm(duration=duration), duration)


@bp.post("/book")
def book() -> Reply:
    """Create a booking, if the slot is still free, and send the invitations."""
    cfg = current_app.config
    form = BookingForm()
    if not form.validate_on_submit():
        return _render_index(form, _valid_duration(form.duration.data), 400)
    if form.website.data:  # honeypot filled: a bot. Pretend all is well and do nothing
        return redirect(url_for("main.index"))

    duration = _valid_duration(form.duration.data)
    start = _parse_slot(form.slot.data or "")
    email = (form.email.data or "").strip().lower()
    now = availability.utcnow()

    # From here on, one database transaction: the first query takes the write lock (see
    # extensions.py), so two visitors cannot book the same slot.
    slots = availability.free_slots(cfg, duration, now)
    if start is None or start not in slots:
        flash("Sorry, that time is no longer available. Please pick another one.", "error")
        return _render_index(form, duration, 409)
    upcoming = db.session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.status == STATUS_CONFIRMED,
            func.lower(Booking.email) == email,
            Booking.start > now,
        )
    )
    if (upcoming or 0) >= cfg["MAX_UPCOMING_PER_EMAIL"]:
        flash("There are already enough upcoming meetings booked with this email address.", "error")
        return _render_index(form, duration, 429)

    manage_token, admin_token = new_token(), new_token()
    domain = cfg["SYSTEM_EMAIL"].rpartition("@")[2] or "justbookmycal"
    name = (form.name.data or "").strip()
    booking = Booking(
        uid=f"{uuid.uuid4()}@{domain}",
        start=start,
        duration=duration,
        name=name,
        email=email,
        message=(form.message.data or "").replace("\r\n", "\n").strip(),
        room=_make_room(name, start),
        booker_token_hash=hash_token(manage_token),
        admin_token_hash=hash_token(admin_token),
    )
    db.session.add(booking)
    db.session.commit()

    manage_url = url_for("main.manage", token=manage_token, _external=True)
    admin_url = url_for("main.admin", token=admin_token, _external=True)
    _notify(booking, invite.METHOD_REQUEST, now, manage_url, admin_url)
    return redirect(manage_url)


# ---------------------------------------------------------------------------------------------
# Booker: /manage/<token>
# ---------------------------------------------------------------------------------------------


@bp.get("/manage/<token>")
def manage(token: str) -> Reply:
    """Details of the meeting, with free slots to move it to and a way to cancel."""
    cfg = current_app.config
    booking = _booking_by_token(Booking.booker_token_hash, token)
    now = availability.utcnow()
    can_change = _is_open(booking, now)
    days = None  # None: the calendar is unavailable right now
    if can_change:
        try:
            days = _days(availability.free_slots(cfg, booking.duration, now, exclude=booking))
        except FeedError:
            flash("The calendar is unavailable at the moment, so moving is not possible.", "error")
    return render_template(
        "manage.html",
        booking=booking,
        token=token,
        can_change=can_change,
        days=days,
        meeting_url=invite.meeting_url(cfg, booking),
        start_local=booking.start.astimezone(_tz()),
    )


@bp.post("/manage/<token>")
def manage_post(token: str) -> Reply:
    """Reschedule into a free slot or cancel."""
    cfg = current_app.config
    booking = _booking_by_token(Booking.booker_token_hash, token)
    now = availability.utcnow()
    url = url_for("main.manage", token=token, _external=True)
    action = request.form.get("action")

    if not _is_open(booking, now):
        flash("This meeting can no longer be changed.", "error")
    elif action == "cancel":
        if request.form.get("confirm"):
            _cancel(booking, now, manage_url=url)
        else:
            flash("Please tick the box to confirm the cancellation.", "error")
    elif action == "reschedule":
        start = _parse_slot(request.form.get("slot", ""))
        slots = availability.free_slots(cfg, booking.duration, now, exclude=booking)
        if start is not None and start in slots:
            _reschedule(booking, start, booking.duration, now, manage_url=url)
        else:
            flash("Sorry, that time is not available. Please pick another one.", "error")
    else:
        abort(400)
    return redirect(url)


@bp.get("/manage/<token>/invite.ics")
def manage_ics(token: str) -> Response:
    """The calendar file, as a fallback if the mail did not arrive."""
    booking = _booking_by_token(Booking.booker_token_hash, token)
    if booking.cancelled:
        abort(404)
    ics = invite.build_ics(
        current_app.config, booking, invite.METHOD_REQUEST, availability.utcnow()
    )
    return Response(
        ics,
        mimetype="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="invite.ics"'},
    )


# ---------------------------------------------------------------------------------------------
# Owner: /admin/<token>
# ---------------------------------------------------------------------------------------------


def _render_admin(
    booking: Booking, values: dict[str, str], conflicts: list[str], status: int = 200
) -> Reply:
    """Render the owner's page."""
    cfg = current_app.config
    return (
        render_template(
            "admin.html",
            booking=booking,
            can_change=_is_open(booking, availability.utcnow()),
            values=values,
            conflicts=conflicts,
            meeting_url=invite.meeting_url(cfg, booking),
            start_local=booking.start.astimezone(_tz()),
        ),
        status,
    )


def _current_values(booking: Booking) -> dict[str, str]:
    """Form defaults: the booking's current time in the owner's time zone."""
    local = booking.start.astimezone(_tz())
    return {
        "date": f"{local:%Y-%m-%d}",
        "time": f"{local:%H:%M}",
        "duration": str(booking.duration),
    }


def _conflicts(booking: Booking, start: datetime, duration: int) -> list[str]:
    """Calendar entries and bookings that overlap with the proposed time, as readable lines."""
    end = start + timedelta(minutes=duration)
    margin = timedelta(days=1)
    busy = availability.gather_busy(
        current_app.config, start - margin, end + margin, exclude=booking
    )
    tz = _tz()
    return [
        f"{b_start.astimezone(tz):%a %d %b, %H:%M}-{b_end.astimezone(tz):%H:%M}"
        for b_start, b_end, _uid in sorted(busy)
        if b_start < end and b_end > start
    ]


def _parse_admin_time(form: Mapping[str, str]) -> tuple[datetime, int] | None:
    """Date, time (owner's time zone) and length from the admin form; None if invalid."""
    try:
        local = datetime.combine(
            date.fromisoformat(form.get("date", "")),
            time.fromisoformat(form.get("time", "")),
            tzinfo=_tz(),
        )
        duration = int(form.get("duration", ""))
    except ValueError:
        return None
    if not MIN_ADMIN_DURATION <= duration <= MAX_ADMIN_DURATION:
        return None
    return local.astimezone(timezone.utc), duration


@bp.get("/admin/<token>")
def admin(token: str) -> Reply:
    """The owner's view of a meeting: reschedule freely or cancel."""
    booking = _booking_by_token(Booking.admin_token_hash, token)
    return _render_admin(booking, _current_values(booking), [])


@bp.post("/admin/<token>")
def admin_post(token: str) -> Reply:
    """Reschedule to any time (rules do not apply) or cancel."""
    booking = _booking_by_token(Booking.admin_token_hash, token)
    now = availability.utcnow()
    url = url_for("main.admin", token=token, _external=True)
    action = request.form.get("action")

    if not _is_open(booking, now):
        flash("This meeting can no longer be changed.", "error")
    elif action == "cancel":
        if request.form.get("confirm"):
            _cancel(booking, now, admin_url=url)
        else:
            flash("Please tick the box to confirm the cancellation.", "error")
    elif action == "reschedule":
        values = {k: request.form.get(k, "") for k in ("date", "time", "duration")}
        parsed = _parse_admin_time(request.form)
        if parsed is None or parsed[0] <= now:
            flash("Please enter a valid time in the future and a length of 5-480 minutes.", "error")
            return _render_admin(booking, values, [], 400)
        start, duration = parsed
        conflicts = _conflicts(booking, start, duration)
        if conflicts and not request.form.get("confirm"):
            return _render_admin(booking, values, conflicts, 409)
        _reschedule(booking, start, duration, now, admin_url=url)
    else:
        abort(400)
    return redirect(url)
