# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for multiple booking profiles: config, routes, bookings, root page and CLI."""

from datetime import datetime
from urllib.error import URLError

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import update

from justbookmycal import availability
from justbookmycal.app import create_app
from justbookmycal.config import CONFIG_SCHEMA, PROFILE_KEYS, AppConfig, profile_config
from justbookmycal.extensions import db
from justbookmycal.models import Booking

from .conftest import TEST_CONFIG
from .test_config import write_config
from .test_views import bookings, first_slot, links, slot_at, slots_of

PROFILES = {
    "me": {"OWNER_NAME": "Jane Doe"},
    "it": {
        "OWNER_NAME": "Jane (IT)",
        "OWNER_EMAIL": "it@example.com",
        "ICS_FEEDS": ["https://cal.example.com/it.ics"],
        "WORKING_HOURS": {"tue": ["10:00-11:00"]},
    },
}


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


def test_profile_keys_match_the_schema():
    schema_keys = set(CONFIG_SCHEMA["properties"]["PROFILES"]["additionalProperties"]["properties"])
    assert schema_keys == set(PROFILE_KEYS)


def test_profile_inherits_and_overrides(tmp_path):
    config = AppConfig.from_yaml(write_config(tmp_path, PROFILES=PROFILES))
    it = profile_config(vars(config) | {"PROFILES": config.PROFILES}, "it")
    assert it["OWNER_NAME"] == "Jane (IT)"  # overridden
    assert it["WORKING_HOURS"] == {"tue": ["10:00-11:00"]}  # overridden
    assert it["TIMEZONE"] == "Europe/Berlin"  # inherited from the top level
    assert it["SYSTEM_EMAIL"] == "noreply@example.com"  # global
    me = profile_config(vars(config) | {"PROFILES": config.PROFILES}, "me")
    assert me["OWNER_EMAIL"] == "jane@example.com"  # inherited
    assert profile_config({"PROFILES": {}, "A": 1}, None) == {"PROFILES": {}, "A": 1}


def test_profiles_are_optional(tmp_path):
    config = AppConfig.from_yaml(write_config(tmp_path))
    assert config.PROFILES == {}
    assert config.DEFAULT_PROFILE == ""
    assert config.ROOT_PAGE == "list"


def test_top_level_owner_may_be_omitted_if_every_profile_sets_it(tmp_path):
    full = {k: {"OWNER_NAME": k, "OWNER_EMAIL": f"{k}@example.com"} for k in ("a", "b")}
    for profile in full.values():
        profile["ICS_FEEDS"] = ["https://cal.example.com/x.ics"]
    path = write_config(tmp_path, OWNER_NAME=None, OWNER_EMAIL=None, ICS_FEEDS=None, PROFILES=full)
    assert set(AppConfig.from_yaml(path).PROFILES) == {"a", "b"}


def test_profile_missing_a_required_setting_is_rejected(tmp_path):
    path = write_config(tmp_path, OWNER_EMAIL=None, PROFILES=PROFILES)
    with pytest.raises(ValueError, match="OWNER_EMAIL must be set in profile 'me'"):
        AppConfig.from_yaml(path)


def test_global_setting_in_a_profile_is_rejected(tmp_path):
    path = write_config(tmp_path, PROFILES={"me": {"SMTP_HOST": "smtp.example.com"}})
    with pytest.raises(ValueError, match="SMTP_HOST"):
        AppConfig.from_yaml(path)


@pytest.mark.parametrize("slug", ["book", "manage", "admin", "static", "Me", "-me", "m e", "m/e"])
def test_bad_or_reserved_slug_is_rejected(tmp_path, slug):
    with pytest.raises(ValueError, match=r"does not match|should not be valid"):
        AppConfig.from_yaml(write_config(tmp_path, PROFILES={slug: {}}))


def test_default_profile_must_exist(tmp_path):
    path = write_config(tmp_path, PROFILES=PROFILES, DEFAULT_PROFILE="nope")
    with pytest.raises(ValueError, match="DEFAULT_PROFILE 'nope'"):
        AppConfig.from_yaml(path)
    assert (
        AppConfig.from_yaml(
            write_config(tmp_path, PROFILES=PROFILES, DEFAULT_PROFILE="it")
        ).DEFAULT_PROFILE
        == "it"
    )


def test_default_profile_without_profiles_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="DEFAULT_PROFILE 'me'"):
        AppConfig.from_yaml(write_config(tmp_path, DEFAULT_PROFILE="me"))


def test_root_page_value_is_checked(tmp_path):
    with pytest.raises(ValueError, match="ROOT_PAGE"):
        AppConfig.from_yaml(write_config(tmp_path, ROOT_PAGE="menu"))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TIMEZONE": "Mars/Olympus"}, "Profile 'it'.*Mars/Olympus"),
        ({"WORKING_HOURS": {"mon": ["17:00-09:00"]}}, "Profile 'it'.*must end after"),
        ({"EVENT_TITLE": "{{ booker_name"}, "Profile 'it'.*not a valid Jinja2"),
        ({"MEETING_URL": "https://meet.example.com/a b"}, "Profile 'it'.*without spaces"),
    ],
)
def test_semantics_are_checked_inside_profiles(tmp_path, override, message):
    path = write_config(tmp_path, PROFILES={"it": override})
    with pytest.raises(ValueError, match=message):
        AppConfig.from_yaml(path)


# ---------------------------------------------------------------------------------------------
# Routes, bookings, root page and CLI
# ---------------------------------------------------------------------------------------------

IT_FEED = "https://cal.example.com/it.ics"
ME_FEED = "https://cal.example.com/max.ics"  # the top-level feed of TEST_CONFIG
TEST_PROFILES = {
    "me": {"OWNER_NAME": "Max Me", "MEETING_URL": "https://meet.example.com/me"},
    "it": {
        "OWNER_NAME": "Max IT",
        "OWNER_EMAIL": "it@example.com",
        "ICS_FEEDS": [IT_FEED],
        "TIMEZONE": "UTC",
        "WORKING_HOURS": {"mon": ["14:00-16:00"]},
        "MEETING_URL": "https://meet.example.com/it",
    },
}


@pytest.fixture(name="papp")
def fixture_papp():
    """An app with the two profiles `me` and `it`, and no default."""
    app = create_app(config_overrides={**TEST_CONFIG, "PROFILES": TEST_PROFILES})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture(name="pclient")
def fixture_pclient(papp: Flask) -> FlaskClient:
    """Test client of the app with profiles."""
    return papp.test_client()


def post_booking(client: FlaskClient, url: str, slot: str):
    """POST the booking form to a booking URL."""
    data = {"slot": slot, "duration": 30, "name": "Jane Doe", "email": "jane@example.com"}
    return client.post(url, data=data)


def recipient(msg) -> str:
    """The address a mail is sent to."""
    return msg["To"].addresses[0].addr_spec


def heading(response) -> str:
    """The h1 of a page."""
    return BeautifulSoup(response.data, "html.parser").select_one("h1").text


def test_each_profile_has_its_own_page(pclient):
    me, it = pclient.get("/me"), pclient.get("/it")
    assert heading(me) == "Book a meeting with Max Me"
    assert heading(it) == "Book a meeting with Max IT"
    assert slot_at(20, 7, 0) in slots_of(me)  # Tuesday 09:00 Berlin: inherited working hours
    assert {s[:10] for s in slots_of(it)} <= {"2026-10-19", "2026-10-26"}  # Mondays only
    assert slot_at(19, 14, 0) in slots_of(it)
    soup = BeautifulSoup(it.data, "html.parser")
    assert soup.select_one("form")["action"] == "/it/book"
    assert "/it?duration=60" in {a["href"] for a in soup.select(".durations a")}
    assert "Times are shown in UTC" in it.text  # its own time zone, not Europe/Berlin


def test_unknown_profile_is_404(pclient, client):
    assert pclient.get("/nope").status_code == 404
    assert pclient.post("/nope/book", data={}).status_code == 404
    assert client.get("/me").status_code == 404  # without profiles, there are no slugs
    assert client.get("/book").status_code == 404


def test_root_shows_the_default_profile(papp, pclient):
    papp.config["DEFAULT_PROFILE"] = "it"
    root = pclient.get("/")
    assert heading(root) == "Book a meeting with Max IT"
    assert BeautifulSoup(root.data, "html.parser").select_one("form")["action"] == "/book"
    assert heading(pclient.get("/it")) == "Book a meeting with Max IT"  # also at its own URL
    assert heading(pclient.get("/me")) == "Book a meeting with Max Me"


def test_booking_in_a_profile_uses_its_settings(pclient, outbox):
    response = post_booking(pclient, "/it/book", slot_at(19, 14, 0))
    assert response.status_code == 302
    [booking] = bookings()
    assert booking.profile == "it"
    assert booking.meeting_url == "https://meet.example.com/it"
    assert {recipient(m) for m in outbox} == {"jane@example.com", "it@example.com"}
    assert all("Max IT" in m["Subject"] for m in outbox)


def test_booking_at_the_root_is_stored_with_the_default_profile(papp, pclient, outbox):
    papp.config["DEFAULT_PROFILE"] = "me"
    assert post_booking(pclient, "/book", slot_at(20, 7, 0)).status_code == 302
    [booking] = bookings()
    assert booking.profile == "me"
    assert booking.meeting_url == "https://meet.example.com/me"


def test_booking_in_single_mode_has_no_profile(client, outbox):
    assert post_booking(client, "/book", first_slot(client)).status_code == 302
    assert bookings()[0].profile is None


def test_a_booking_blocks_the_slot_in_every_profile(pclient, outbox):
    assert slot_at(19, 14, 0) in slots_of(pclient.get("/me"))
    post_booking(pclient, "/it/book", slot_at(19, 14, 0))
    bookings()
    assert slot_at(19, 14, 0) not in slots_of(pclient.get("/me"))
    assert slot_at(19, 14, 0) not in slots_of(pclient.get("/it"))


def test_slot_of_another_profile_cannot_be_booked(pclient, outbox):
    # Tuesday is bookable at /me, but not at /it
    assert post_booking(pclient, "/it/book", slot_at(20, 7, 0)).status_code == 409
    assert bookings() == []


def test_manage_and_admin_use_the_profile_of_the_booking(pclient, outbox):
    post_booking(pclient, "/it/book", slot_at(19, 14, 0))
    pclient.get("/it")  # clears the flash
    found = links(outbox)
    outbox.clear()

    page = pclient.get(found["manage"])
    assert heading(page) == "Your meeting with Max IT"
    offered = slots_of(page)
    assert slot_at(19, 14, 0) in offered
    assert all(datetime.fromisoformat(s.replace("Z", "+00:00")).weekday() == 0 for s in offered)

    assert "Times are in UTC" in pclient.get(found["admin"]).text

    pclient.post(found["manage"], data={"action": "reschedule", "slot": slot_at(19, 15, 0)})
    assert {recipient(m) for m in outbox} == {"jane@example.com", "it@example.com"}
    assert all("Max IT" in m["Subject"] for m in outbox)
    bodies = [m.get_body(("plain",)).get_content() for m in outbox]
    assert all("https://meet.example.com/it" in body for body in bodies)


@pytest.mark.parametrize("profile", [None, "gone"])
def test_booking_without_or_with_removed_profile_uses_top_level_settings(
    pclient, outbox, caplog, profile
):
    post_booking(pclient, "/it/book", slot_at(19, 14, 0))
    pclient.get("/it")
    found = links(outbox)
    outbox.clear()
    db.session.execute(update(Booking).values(profile=profile))
    db.session.commit()
    db.session.remove()

    page = pclient.get(found["manage"])
    assert heading(page) == "Your meeting with Max Mehl"  # OWNER_NAME of the top level
    assert ("is not configured any more" in caplog.text) == (profile == "gone")
    pclient.post(found["manage"], data={"action": "cancel", "confirm": "1"})
    assert bookings()[0].cancelled
    assert {recipient(m) for m in outbox} == {"jane@example.com", "max@example.com"}


# --- Root page ---------------------------------------------------------------------------------


def test_root_lists_the_profiles_by_default(pclient):
    response = pclient.get("/")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert {(a["href"], a.text) for a in soup.select("li a")} == {
        ("/me", "Book a meeting with Max Me"),
        ("/it", "Book a meeting with Max IT"),
    }
    assert "Max Mehl" not in response.text  # the top-level name belongs to no profile


def test_root_can_be_a_placeholder(papp, pclient):
    papp.config["ROOT_PAGE"] = "placeholder"
    response = pclient.get("/")
    assert response.status_code == 200
    assert "There is no booking page here." in response.text
    assert "Max" not in response.text
    assert "/me" not in response.text


def test_default_profile_wins_over_root_page(papp, pclient):
    papp.config.update(ROOT_PAGE="placeholder", DEFAULT_PROFILE="me")
    assert heading(pclient.get("/")) == "Book a meeting with Max Me"


def test_nothing_can_be_booked_at_the_root_menu(pclient):
    assert post_booking(pclient, "/book", slot_at(19, 14, 0)).status_code == 404
    assert bookings() == []


def test_error_pages_use_the_root_settings(papp, pclient):
    response = pclient.get("/nope")
    assert response.status_code == 404
    assert "Back to the booking page" in response.text
    assert "· Max Mehl</title>" in response.text
    papp.config["DEFAULT_PROFILE"] = "it"
    assert "· Max IT</title>" in pclient.get("/nope").text


# --- Feeds -------------------------------------------------------------------------------------


@pytest.fixture(name="downloads")
def fixture_downloads(monkeypatch, feed) -> list[str]:
    """The feed URLs downloaded; the feed of `it` is broken."""
    urls: list[str] = []

    def download(url: str, timeout: int) -> bytes:
        urls.append(url)
        if url == IT_FEED:
            raise URLError("down")  # noqa: EM101
        return feed.data

    monkeypatch.setattr(availability, "_download", download)
    return urls


def test_a_page_only_loads_the_feeds_of_its_profile(pclient, downloads):
    assert pclient.get("/me").status_code == 200
    assert downloads == [ME_FEED]  # the broken feed of `it` neither delays nor breaks it
    assert pclient.get("/it").status_code == 503  # its own calendar is unavailable


def test_the_root_loads_the_feeds_of_the_default_profile(papp, pclient, downloads):
    papp.config["DEFAULT_PROFILE"] = "me"
    pclient.get("/")
    assert downloads == [ME_FEED]


def test_manage_pages_warm_all_feeds_once(pclient, downloads):
    pclient.get("/manage/unknown")
    assert sorted(downloads) == sorted([ME_FEED, IT_FEED])


# --- CLI ---------------------------------------------------------------------------------------


def test_slots_command_takes_a_profile(papp):
    result = papp.test_cli_runner().invoke(args=["slots", "--profile", "it", "--duration", "60"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:2] == ["Mon 2026-10-19 14:00 UTC", "Mon 2026-10-19 14:15 UTC"]
    assert all(line.startswith("Mon") for line in lines)  # its only working day


def test_cli_needs_a_profile_if_there_is_no_default(papp):
    result = papp.test_cli_runner().invoke(args=["busy"])
    assert result.exit_code == 2
    assert "choose one of: me, it" in result.output
    papp.config["DEFAULT_PROFILE"] = "me"
    assert papp.test_cli_runner().invoke(args=["busy"]).exit_code == 0


def test_cli_rejects_an_unknown_profile(papp):
    result = papp.test_cli_runner().invoke(args=["slots", "--profile", "nope"])
    assert result.exit_code == 2
    assert "unknown profile 'nope'" in result.output
