# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for feed parsing and the slot engine."""

from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from zoneinfo import ZoneInfo

import pytest

from justbookmycal import availability
from justbookmycal.availability import FeedError, Rules, compute_slots, get_busy
from justbookmycal.config import AppConfig

from .conftest import FIXTURES, NOW, TEST_CONFIG, Feed

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def cfg(**changes) -> dict:
    """The test config with some changes."""
    config = {k: getattr(AppConfig, k) for k in dir(AppConfig) if k.isupper()}
    return {**config, **TEST_CONFIG, **changes}


def berlin(*args: int) -> datetime:
    """Berlin wall-clock time."""
    return datetime(*args, tzinfo=BERLIN)


def busy_local(feed: Feed, start: datetime, end: datetime) -> list[tuple[str, str, str]]:
    """Busy blocks from the fixture feed as readable (start, end, uid) in Berlin time."""
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    return sorted(
        (s.astimezone(BERLIN).strftime("%a %H:%M"), e.astimezone(BERLIN).strftime("%H:%M"), uid)
        for s, e, uid in get_busy(cfg(), start, end)
    )


# ---------------------------------------------------------------------------------------------
# Busy times from feeds
# ---------------------------------------------------------------------------------------------


def test_recurrence_exdate_moved_instance_and_filters(feed):
    busy = busy_local(feed, berlin(2026, 10, 19), berlin(2026, 11, 3))
    assert sorted(busy) == sorted(
        [
            # The weekly Monday 10:00 of 2026-10-19 was moved to 14:00
            ("Mon 14:00", "15:00", "weekly"),
            # 2026-10-26 is an EXDATE; 2026-11-02 is a regular instance
            ("Mon 10:00", "11:00", "weekly"),
            # Opaque all-day event: the whole Thursday
            ("Thu 00:00", "00:00", "vacation"),
            # Event given in UTC, shown in Berlin time
            ("Fri 15:00", "16:00", "utc-event"),
            # Not listed on purpose: TRANSPARENT event, CANCELLED event, TRANSPARENT all-day event
        ]
    )


def test_busy_blocks_exact(feed):
    """Same as above but with exact expectations, day by day."""
    busy = busy_local(feed, berlin(2026, 10, 19), berlin(2026, 11, 3))
    assert ("Mon 14:00", "15:00", "weekly") in busy  # moved instance
    assert ("Mon 10:00", "11:00", "weekly") in busy  # 2026-11-02
    assert ("Thu 00:00", "00:00", "vacation") in busy  # all-day, opaque
    assert ("Fri 15:00", "16:00", "utc-event") in busy  # UTC converted to Berlin
    assert len(busy) == 4  # nothing for: original 10:00 on the 19th, EXDATE on the 26th,
    # the transparent and the cancelled event, the transparent all-day event


def test_events_with_the_excluded_uid_are_ignored(feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    busy = get_busy(cfg(), berlin(2026, 10, 19), berlin(2026, 10, 25), exclude_uid="utc-event")
    assert "utc-event" not in {uid for _, _, uid in busy}
    assert "weekly" in {uid for _, _, uid in busy}


def test_feed_is_cached(feed):
    get_busy(cfg(), NOW, NOW + timedelta(days=1))
    get_busy(cfg(), NOW, NOW + timedelta(days=1))
    assert feed.downloads == 1


def test_stale_cache_is_used_when_the_feed_fails(feed):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    config = cfg(FEED_CACHE_SECONDS=0)  # always refresh
    first = get_busy(config, berlin(2026, 10, 19), berlin(2026, 10, 25))
    feed.error = URLError("down")
    assert get_busy(config, berlin(2026, 10, 19), berlin(2026, 10, 25)) == first
    assert feed.downloads == 2


def test_failing_feed_without_cache_raises(feed):
    feed.error = URLError("down")
    with pytest.raises(FeedError, match="down"):
        get_busy(cfg(), NOW, NOW + timedelta(days=1))


def test_garbage_feed_raises(feed):
    feed.data = b"<html>not a calendar</html>"
    with pytest.raises(FeedError):
        get_busy(cfg(), NOW, NOW + timedelta(days=1))


def test_cached_feed_expires_after_a_day(feed, monkeypatch):
    feed.data = (FIXTURES / "busy.ics").read_bytes()
    config = cfg(FEED_CACHE_SECONDS=0)
    get_busy(config, NOW, NOW + timedelta(days=1))
    feed.error = URLError("down")
    real = availability.time.monotonic()
    monkeypatch.setattr(availability.time, "monotonic", lambda: real + 25 * 3600)
    with pytest.raises(FeedError):
        get_busy(config, NOW, NOW + timedelta(days=1))


# ---------------------------------------------------------------------------------------------
# Slot engine
# ---------------------------------------------------------------------------------------------


def rules(**changes) -> Rules:
    """Rules for tests: Mon-Fri 09:00-12:00 and 13:00-17:00 in Berlin, no notice, 14 days."""
    options = {
        "WORKING_HOURS": {
            d: ["09:00-12:00", "13:00-17:00"] for d in ("mon", "tue", "wed", "thu", "fri")
        },
        "MIN_NOTICE_HOURS": 0,
        "HORIZON_DAYS": 14,
        **changes,
    }
    return Rules.from_config(cfg(**options))


def local_slots(slots: list[datetime]) -> list[str]:
    """Slots as 'Mon 09:00' in Berlin time."""
    return [s.astimezone(BERLIN).strftime("%a %H:%M") for s in slots]


def test_normal_day_has_slots_only_in_working_windows():
    # Monday 2026-10-19, 08:00 Berlin, ask only for that day via the horizon
    slots = compute_slots(rules(HORIZON_DAYS=1), 60, [], NOW)
    monday = [s for s in local_slots(slots) if s.startswith("Mon")][:14]
    assert monday[0] == "Mon 09:00"
    assert "Mon 11:00" in monday  # 11:00-12:00 still fits
    assert "Mon 11:15" not in local_slots(slots)  # would run into the lunch break
    assert "Mon 12:00" not in local_slots(slots)
    assert "Mon 13:00" in local_slots(slots)
    assert "Mon 16:00" in local_slots(slots)
    assert "Mon 16:15" not in local_slots(slots)


def test_weekends_and_unlisted_days_have_no_slots():
    slots = compute_slots(rules(), 30, [], NOW)
    assert not [s for s in local_slots(slots) if s.startswith(("Sat", "Sun"))]


def test_step_defines_the_start_times():
    slots = local_slots(compute_slots(rules(SLOT_STEP_MINUTES=30), 30, [], NOW))
    assert "Mon 09:30" in slots
    assert "Mon 09:15" not in slots


def test_busy_blocks_overlapping_slots_only():
    busy = [(berlin(2026, 10, 19, 10, 0), berlin(2026, 10, 19, 11, 0), "x")]
    slots = local_slots(compute_slots(rules(HORIZON_DAYS=1), 30, busy, NOW))
    assert "Mon 09:30" in slots  # ends exactly when the event starts
    assert "Mon 09:45" not in slots
    assert "Mon 10:45" not in slots
    assert "Mon 11:00" in slots  # starts exactly when the event ends


def test_buffer_before_keeps_distance_after_events_only():
    # Event 10:00-11:00. 30 minutes of buffer before a meeting: the previous event must have ended
    # at least that long before the meeting starts, but a meeting may end right when one starts.
    busy = [(berlin(2026, 10, 19, 10, 0), berlin(2026, 10, 19, 11, 0), "x")]
    slots = local_slots(
        compute_slots(rules(BUFFER_MINUTES_BEFORE=30, HORIZON_DAYS=1), 30, busy, NOW)
    )
    assert "Mon 09:30" in slots  # ends exactly when the event starts: no buffer needed after
    assert "Mon 11:00" not in slots  # would start right after the event
    assert "Mon 11:15" not in slots
    assert "Mon 11:30" in slots  # 30 minutes after the event ended


def test_buffer_after_keeps_distance_before_events_only():
    busy = [(berlin(2026, 10, 19, 10, 0), berlin(2026, 10, 19, 11, 0), "x")]
    slots = local_slots(
        compute_slots(rules(BUFFER_MINUTES_AFTER=30, HORIZON_DAYS=1), 30, busy, NOW)
    )
    assert "Mon 09:00" in slots  # ends 09:30, the event starts 30 minutes later
    assert "Mon 09:15" not in slots
    assert "Mon 09:30" not in slots  # would end right when the event starts
    assert "Mon 11:00" in slots  # starting right after an event needs no buffer before


def test_buffers_can_differ():
    busy = [(berlin(2026, 10, 19, 10, 0), berlin(2026, 10, 19, 11, 0), "x")]
    slots = local_slots(
        compute_slots(
            rules(BUFFER_MINUTES_BEFORE=15, BUFFER_MINUTES_AFTER=30, HORIZON_DAYS=1), 30, busy, NOW
        )
    )
    assert "Mon 09:00" in slots
    assert "Mon 09:15" not in slots
    assert "Mon 11:00" not in slots
    assert "Mon 11:15" in slots


def test_old_buffer_setting_is_rejected(tmp_path):
    from .test_config import write_config

    with pytest.raises(ValueError, match="BUFFER_MINUTES"):
        AppConfig.from_yaml(write_config(tmp_path, BUFFER_MINUTES=15))


def test_minimum_notice():
    # Now is Monday 08:00 Berlin; 3 hours of notice means the first slot is 11:00
    slots = local_slots(compute_slots(rules(MIN_NOTICE_HOURS=3), 30, [], NOW))
    assert slots[0] == "Mon 11:00"


def test_booking_horizon():
    slots = compute_slots(rules(HORIZON_DAYS=7), 30, [], NOW)
    assert max(slots) <= NOW + timedelta(days=7)
    # 2026-10-26 09:00 Berlin is 08:00Z, two hours after NOW + 7 days = 06:00Z, so it is cut off
    assert berlin(2026, 10, 26, 9, 0).astimezone(UTC) not in slots
    assert berlin(2026, 10, 23, 16, 30).astimezone(UTC) in slots


def test_multiple_windows_per_day():
    slots = local_slots(compute_slots(rules(), 60, [], NOW))
    assert "Mon 12:00" not in slots
    assert "Mon 11:00" in slots
    assert "Mon 13:00" in slots


def test_dst_end_day_has_an_extra_hour():
    """On 2026-10-25 the clocks go back at 03:00 -> 02:00: 01:00-05:00 are five real hours."""
    config = cfg(
        WORKING_HOURS={"sun": ["01:00-05:00"]},
        MIN_NOTICE_HOURS=0,
        HORIZON_DAYS=7,
        SLOT_STEP_MINUTES=60,
    )
    slots = compute_slots(Rules.from_config(config), 60, [], NOW)
    assert [s.astimezone(UTC).strftime("%d %H:%M") for s in slots] == [
        "24 23:00",
        "25 00:00",
        "25 01:00",
        "25 02:00",
        "25 03:00",
    ]


def test_dst_start_day_loses_an_hour():
    """On 2026-03-29 the clocks go forward at 02:00 -> 03:00: 01:00-05:00 are three real hours."""
    config = cfg(
        WORKING_HOURS={"sun": ["01:00-05:00"]},
        MIN_NOTICE_HOURS=0,
        HORIZON_DAYS=6,
        SLOT_STEP_MINUTES=60,
    )
    now = datetime(2026, 3, 25, 12, 0, tzinfo=UTC)
    slots = compute_slots(Rules.from_config(config), 60, [], now)
    assert len(slots) == 3


def test_free_slots_combine_feed_and_database(app, feed):
    from justbookmycal.extensions import db
    from justbookmycal.models import Booking

    feed.data = (FIXTURES / "busy.ics").read_bytes()
    config = {**app.config, "WORKING_HOURS": {"mon": ["09:00-17:00"]}, "HORIZON_DAYS": 1}
    before = availability.free_slots(config, 60, NOW)
    assert berlin(2026, 10, 19, 14, 0).astimezone(UTC) not in before  # moved weekly sync
    assert berlin(2026, 10, 19, 12, 0).astimezone(UTC) in before

    booking = Booking(
        uid="b1",
        start=berlin(2026, 10, 19, 12, 0),
        duration=60,
        name="A",
        email="a@example.com",
        room="r",
        booker_token_hash="1",
        admin_token_hash="2",
    )
    db.session.add(booking)
    db.session.commit()
    after = availability.free_slots(config, 60, NOW)
    assert berlin(2026, 10, 19, 12, 0).astimezone(UTC) not in after
    # The booked meeting does not block itself when it is being moved
    moving = availability.free_slots(config, 60, NOW, exclude=booking)
    assert berlin(2026, 10, 19, 12, 0).astimezone(UTC) in moving
    db.session.remove()
