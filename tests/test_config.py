# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for loading and validating the configuration."""

from pathlib import Path

import pytest
import yaml

from justbookmycal.app import create_app
from justbookmycal.config import AppConfig

VALID = {
    "SECRET_KEY": "s" * 32,
    "OWNER_NAME": "Jane Doe",
    "OWNER_EMAIL": "jane@example.com",
    "SYSTEM_EMAIL": "noreply@example.com",
    "ICS_FEEDS": ["https://cal.example.com/jane.ics"],
    "TIMEZONE": "Europe/Berlin",
    "WORKING_HOURS": {"mon": ["09:00-12:00", "13:00-17:00"]},
}


def write_config(tmp_path: Path, **changes) -> Path:
    """Write a config file based on VALID with some keys changed or (None) removed."""
    data = {**VALID, **changes}
    data = {k: v for k, v in data.items() if v is not None}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_valid_config_loads_with_defaults(tmp_path):
    config = AppConfig.from_yaml(write_config(tmp_path))
    assert config.OWNER_NAME == "Jane Doe"
    assert config.WORKING_HOURS == {"mon": ["09:00-12:00", "13:00-17:00"]}
    assert config.DURATIONS == [15, 30, 60]  # default
    assert config.MAX_UPCOMING_PER_EMAIL == 2  # default


def test_example_config_is_valid_apart_from_the_secret(tmp_path):
    example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    example["SECRET_KEY"] = "s" * 32
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(example), encoding="utf-8")
    AppConfig.from_yaml(path)


def test_unknown_time_zone_is_rejected_with_a_clear_message(tmp_path):
    with pytest.raises(ValueError, match=r"Mars/Olympus.*IANA"):
        AppConfig.from_yaml(write_config(tmp_path, TIMEZONE="Mars/Olympus"))


@pytest.mark.parametrize("window", ["9-17", "09:00-25:00", "09:00 - 17:00", "09:00"])
def test_malformed_working_hours_are_rejected(tmp_path, window):
    with pytest.raises(ValueError, match="does not match"):
        AppConfig.from_yaml(write_config(tmp_path, WORKING_HOURS={"mon": [window]}))


def test_working_window_must_end_after_it_starts(tmp_path):
    with pytest.raises(ValueError, match="must end after it starts"):
        AppConfig.from_yaml(write_config(tmp_path, WORKING_HOURS={"mon": ["17:00-09:00"]}))


def test_unknown_weekday_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="funday"):
        AppConfig.from_yaml(write_config(tmp_path, WORKING_HOURS={"funday": ["09:00-10:00"]}))


@pytest.mark.parametrize(
    "missing", ["SECRET_KEY", "OWNER_NAME", "OWNER_EMAIL", "SYSTEM_EMAIL", "ICS_FEEDS"]
)
def test_required_keys(tmp_path, missing):
    with pytest.raises(ValueError, match=missing):
        AppConfig.from_yaml(write_config(tmp_path, **{missing: None}))


def test_short_secret_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="too short"):
        AppConfig.from_yaml(write_config(tmp_path, SECRET_KEY="short"))


def test_feed_must_be_http_url(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        AppConfig.from_yaml(write_config(tmp_path, ICS_FEEDS=["file:///etc/passwd"]))


def test_broken_template_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="EVENT_TITLE is not a valid Jinja2 template"):
        AppConfig.from_yaml(write_config(tmp_path, EVENT_TITLE="{{ booker_name"))


def test_app_refuses_to_start_without_secret_key():
    with pytest.raises(ValueError, match="SECRET_KEY"):
        create_app(config_overrides={"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


def test_app_starts_from_yaml(tmp_path):
    app = create_app(
        yaml_config_path=str(write_config(tmp_path)),
        config_overrides={"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"},
    )
    assert app.config["OWNER_NAME"] == "Jane Doe"


def test_system_name_is_optional_and_checked(tmp_path):
    assert AppConfig.from_yaml(write_config(tmp_path)).SYSTEM_NAME == ""
    assert AppConfig.from_yaml(write_config(tmp_path, SYSTEM_NAME="Jane (Booking)")).SYSTEM_NAME
    for bad in ('Jane "Bot"', "Jane\nBcc: x@example.com"):
        with pytest.raises(ValueError, match="does not match"):
            AppConfig.from_yaml(write_config(tmp_path, SYSTEM_NAME=bad))
