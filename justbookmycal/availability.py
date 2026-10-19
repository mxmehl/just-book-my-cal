# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Busy times from ICS feeds and the free-slot engine."""

import logging
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
from sqlalchemy import select

from .config import DAYS
from .extensions import db
from .models import STATUS_CONFIRMED, Booking

Busy = tuple[datetime, datetime, str]  # start (UTC), end (UTC), UID of the originating event

MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_STALE_SECONDS = 24 * 3600  # a cached feed older than this is not trusted any more
MAX_BOOKING_DURATION = timedelta(days=1)  # bound for the DB pre-filter in gather_busy()

# Parsed feeds per URL: (time fetched, calendar).
# ponytail: per process, so every gunicorn worker fetches on its own. Fine for one person's feed.
_cache: dict[str, tuple[float, icalendar.Calendar]] = {}


class FeedError(Exception):
    """A calendar feed could not be fetched or parsed and no usable cached copy exists."""


def utcnow() -> datetime:
    """Return the current time (UTC). A function of its own so tests can replace it."""
    return datetime.now(tz=timezone.utc)


def _download(url: str, timeout: int) -> bytes:
    """Download a feed. Replaced in tests."""
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    request = urllib.request.Request(url, headers={"User-Agent": "just-book-my-cal"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        msg = "feed is too large"
        raise ValueError(msg)
    return data


def _load_feed(url: str, ttl: int, timeout: int) -> icalendar.Calendar:
    """Return a parsed feed from the cache or the network; fall back to a stale copy on errors.

    Raises:
        FeedError: if fetching fails and there is no cached copy younger than MAX_STALE_SECONDS
    """
    now = time.monotonic()
    cached = _cache.get(url)
    if cached and now - cached[0] < ttl:
        return cached[1]
    try:
        calendar = icalendar.Calendar.from_ical(_download(url, timeout))
    except (OSError, ValueError) as e:
        if cached and now - cached[0] < MAX_STALE_SECONDS:
            logging.warning("Could not refresh feed %s (%s), using the cached copy", url, e)
            return cached[1]
        msg = f"Could not load calendar feed: {e}"
        raise FeedError(msg) from e
    _cache[url] = (now, calendar)
    return calendar


def prefetch(cfg: Mapping[str, Any]) -> None:
    """Load all feeds into the cache.

    Called before the first database access of a request: with SQLite, the first query takes the
    write lock (see extensions.py), and a slow download must not happen while it is held.

    Raises:
        FeedError: if a feed cannot be loaded
    """
    for url in cfg["ICS_FEEDS"]:
        _load_feed(url, cfg["FEED_CACHE_SECONDS"], cfg["FEED_TIMEOUT_SECONDS"])


def _to_datetime(value: date | datetime, tz: ZoneInfo) -> datetime:
    """Make an aware datetime: all-day dates start at midnight, floating times get `tz`."""
    if not isinstance(value, datetime):
        return datetime.combine(value, dtime.min, tzinfo=tz)
    return value if value.tzinfo else value.replace(tzinfo=tz)


def get_busy(
    cfg: Mapping[str, Any], start: datetime, end: datetime, exclude_uid: str | None = None
) -> list[Busy]:
    """Return the busy intervals of all feeds between `start` and `end`.

    Events marked TRANSP:TRANSPARENT or STATUS:CANCELLED do not block. A missing TRANSP means
    OPAQUE, as the standard says, which also applies to all-day events.

    Args:
        cfg: app config
        start: begin of the window (aware)
        end: end of the window (aware)
        exclude_uid: ignore events with this UID (the meeting being rescheduled)

    Raises:
        FeedError: if a feed cannot be loaded
    """
    tz = ZoneInfo(cfg["TIMEZONE"])
    busy: list[Busy] = []
    for url in cfg["ICS_FEEDS"]:
        calendar = _load_feed(url, cfg["FEED_CACHE_SECONDS"], cfg["FEED_TIMEOUT_SECONDS"])
        for event in recurring_ical_events.of(calendar).between(start, end):
            if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
                continue
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            uid = str(event.get("UID", ""))
            if exclude_uid and uid == exclude_uid:
                continue
            ev_start, ev_end = _to_datetime(event.start, tz), _to_datetime(event.end, tz)
            if ev_end <= ev_start:  # zero-length events (reminders etc.) block nothing
                continue
            busy.append((ev_start.astimezone(timezone.utc), ev_end.astimezone(timezone.utc), uid))
    return busy


def gather_busy(
    cfg: Mapping[str, Any], start: datetime, end: datetime, exclude: Booking | None = None
) -> list[Busy]:
    """Busy intervals from the feeds plus confirmed bookings, which the feeds may not show yet.

    Args:
        cfg: app config
        start: begin of the window (aware)
        end: end of the window (aware)
        exclude: a booking that is being moved and must not block itself

    Raises:
        FeedError: if a feed cannot be loaded
    """
    busy = get_busy(cfg, start, end, exclude_uid=exclude.uid if exclude else None)
    query = select(Booking).where(
        Booking.status == STATUS_CONFIRMED,
        Booking.start < end,
        Booking.start > start - MAX_BOOKING_DURATION,
    )
    for booking in db.session.scalars(query):
        if booking.end > start and not (exclude and booking.id == exclude.id):
            busy.append((booking.start, booking.end, booking.uid))
    return busy


@dataclass(frozen=True)
class Rules:
    """When the owner can be booked."""

    tz: ZoneInfo
    hours: dict[int, list[tuple[dtime, dtime]]]  # weekday (Monday=0) -> sorted windows
    step: timedelta
    notice: timedelta
    horizon: timedelta
    buffer_before: timedelta  # free time required before a meeting
    buffer_after: timedelta  # free time required after a meeting

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "Rules":
        """Build the rules from the app config."""
        hours = {
            DAYS.index(day): sorted(
                (dtime.fromisoformat(w.split("-")[0]), dtime.fromisoformat(w.split("-")[1]))
                for w in windows
            )
            for day, windows in cfg["WORKING_HOURS"].items()
        }
        return cls(
            tz=ZoneInfo(cfg["TIMEZONE"]),
            hours=hours,
            step=timedelta(minutes=cfg["SLOT_STEP_MINUTES"]),
            notice=timedelta(hours=cfg["MIN_NOTICE_HOURS"]),
            horizon=timedelta(days=cfg["HORIZON_DAYS"]),
            buffer_before=timedelta(minutes=cfg["BUFFER_MINUTES_BEFORE"]),
            buffer_after=timedelta(minutes=cfg["BUFFER_MINUTES_AFTER"]),
        )


def compute_slots(rules: Rules, duration: int, busy: list[Busy], now: datetime) -> list[datetime]:
    """Return the start times (UTC) at which a meeting of `duration` minutes can be booked.

    Pure function: no I/O. Windows are laid out per day in the owner's time zone and converted to
    UTC before stepping, so DST changes shorten or lengthen a day correctly.

    Args:
        rules: working hours and constraints
        duration: meeting length in minutes
        busy: busy intervals (UTC)
        now: current time (aware)
    """
    length = timedelta(minutes=duration)
    earliest, latest = now + rules.notice, now + rules.horizon
    # ponytail: linear scan of all busy blocks per candidate. Fine for a few hundred events; sort
    # and bisect if a feed ever gets huge.
    slots: list[datetime] = []
    day = now.astimezone(rules.tz).date()
    last_day = latest.astimezone(rules.tz).date()
    while day <= last_day:
        for window_start, window_end in rules.hours.get(day.weekday(), []):
            slot = datetime.combine(day, window_start, tzinfo=rules.tz).astimezone(timezone.utc)
            end = datetime.combine(day, window_end, tzinfo=rules.tz).astimezone(timezone.utc)
            while slot + length <= end:
                # The meeting plus its buffers must not touch any busy block
                guarded_start, guarded_end = (
                    slot - rules.buffer_before,
                    slot + length + rules.buffer_after,
                )
                free = not any(
                    guarded_start < b_end and guarded_end > b_start for b_start, b_end, _ in busy
                )
                if earliest <= slot <= latest and free:
                    slots.append(slot)
                slot += rules.step
        day += timedelta(days=1)
    return slots


def free_slots(
    cfg: Mapping[str, Any], duration: int, now: datetime, exclude: Booking | None = None
) -> list[datetime]:
    """Free slots for a meeting length, considering feeds and bookings.

    Args:
        cfg: app config
        duration: meeting length in minutes
        now: current time (aware)
        exclude: a booking that is being moved and must not block itself

    Raises:
        FeedError: if a feed cannot be loaded
    """
    rules = Rules.from_config(cfg)
    margin = timedelta(days=1)
    busy = gather_busy(cfg, now - margin, now + rules.horizon + margin, exclude)
    return compute_slots(rules, duration, busy, now)
