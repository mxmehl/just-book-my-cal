# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration for just-book-my-cal."""

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import FormatChecker, validate
from jsonschema.exceptions import ValidationError
from platformdirs import user_config_path

APP_NAME = "justbookmycal"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Settings a profile may override; all others are global. Keep in sync with config_schema.json.
PROFILE_KEYS = (
    "OWNER_NAME",
    "OWNER_EMAIL",
    "TIMEZONE",
    "ICS_FEEDS",
    "DURATIONS",
    "WORKING_HOURS",
    "SLOT_STEP_MINUTES",
    "MIN_NOTICE_HOURS",
    "HORIZON_DAYS",
    "BUFFER_MINUTES_BEFORE",
    "BUFFER_MINUTES_AFTER",
    "MEETING_URL",
    "REMINDER_MINUTES",
    "EVENT_TITLE",
    "EVENT_DESCRIPTION",
    "EMAIL_BODY_BOOKER",
    "EMAIL_BODY_OWNER",
    "SYSTEM_NAME",
)
# Without these, there is nobody to book: each profile needs them, directly or by inheritance
REQUIRED_PROFILE_KEYS = ("OWNER_NAME", "OWNER_EMAIL", "ICS_FEEDS")

# Variables of the MEETING_URL template, with an example of each (used to check the template)
MEETING_URL_SAMPLE = {
    "owner": "MaxMehl",
    "booker": "JaneDoe",
    "date": "20261020",
    "random": "k7p2x",
}

DEFAULT_EVENT_TITLE = "{{ booker_name }} / {{ owner_name }} ({{ duration }} min)"
DEFAULT_EVENT_DESCRIPTION = (
    "{% if booker_message %}{{ booker_message }}\n\n{% endif %}Join: {{ meeting_url }}"
)
DEFAULT_EMAIL_BODY_BOOKER = """\
Hi {{ booker_name }},

your meeting with {{ owner_name }} is {{ status }}.

When: {{ start_local }} ({{ start_utc }})
Length: {{ duration }} minutes
Join: {{ meeting_url }}

The calendar invitation is attached.
{% if manage_url %}
If you need to change the date or cancel, use this private link (do not share it):
{{ manage_url }}
{% endif %}"""
DEFAULT_EMAIL_BODY_OWNER = """\
Meeting with {{ booker_name }} <{{ booker_email }}> is {{ status }}.

When: {{ start_local }} ({{ start_utc }})
Length: {{ duration }} minutes
Join: {{ meeting_url }}
{% if booker_message %}
Message:
{{ booker_message }}
{% endif %}{% if admin_url %}
Reschedule freely or cancel with your private admin link (do not share it):
{{ admin_url }}
{% endif %}"""


def _load_config_schema() -> dict:
    """Load the configuration schema from JSON file.

    Returns:
        Dictionary with the configuration schema
    """
    schema_path = Path(__file__).parent / "config_schema.json"
    with schema_path.open(encoding="utf-8") as f:
        return json.load(f)


CONFIG_SCHEMA = _load_config_schema()


def profile_config(cfg: Mapping[str, Any], slug: str | None) -> dict[str, Any]:
    """The settings of a profile: the global config with the profile's overrides on top.

    Built on every call from the live config, so later changes to `cfg` are seen.

    Args:
        cfg: the app config
        slug: the profile, or None for the top-level settings alone

    Returns:
        A new dict, safe to pass to `availability` and `invite` in place of the app config
    """
    return {**cfg, **cfg.get("PROFILES", {})[slug]} if slug else dict(cfg)


def default_config_path() -> str:
    """Where the configuration file is looked for if no path is given.

    This is the user config directory of the platform, e.g. `~/.config/justbookmycal/config.yaml`
    on Linux or `~/Library/Application Support/justbookmycal/config.yaml` on macOS. Unlike a
    relative path, it does not depend on the directory the app is started from. Nothing is
    created.
    """
    return str(user_config_path(appname=APP_NAME) / "config.yaml")


def default_database_uri() -> str:
    """Database URI used if the config sets no DATABASE_URI: SQLite next to the config file.

    The directory is created if needed, because SQLite cannot do that.
    """
    directory = user_config_path(appname=APP_NAME)
    directory.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{directory / 'justbookmycal.db'}"


class AppConfig:  # pylint: disable=too-few-public-methods
    """Flask configuration from YAML file with some defaults."""

    # App settings
    DATABASE_URI: str = ""  # Empty here; app setup falls back to SQLite in the user config dir.
    SECRET_KEY: str = ""  # Empty here; production startup requires a non-empty value.
    TRUSTED_PROXIES: int = 1  # Number of reverse proxies in front (ProxyFix); 0 disables

    # Owner
    OWNER_NAME: str = ""
    OWNER_EMAIL: str = ""
    TIMEZONE: str = "UTC"  # IANA time zone in which the working hours are defined

    # Calendar feeds
    ICS_FEEDS: ClassVar[list[str]] = []
    FEED_CACHE_SECONDS: int = 300
    FEED_TIMEOUT_SECONDS: int = 10

    # Booking rules
    DURATIONS: ClassVar[list[int]] = [15, 30, 60]
    WORKING_HOURS: ClassVar[dict[str, list[str]]] = {
        day: ["09:00-17:00"] for day in ("mon", "tue", "wed", "thu", "fri")
    }
    SLOT_STEP_MINUTES: int = 15
    MIN_NOTICE_HOURS: float = 12
    HORIZON_DAYS: int = 30
    BUFFER_MINUTES_BEFORE: int = 0  # free time required before a meeting
    BUFFER_MINUTES_AFTER: int = 0  # free time required after a meeting
    MAX_UPCOMING_PER_EMAIL: int = 2

    # Invitation
    MEETING_URL: str = "https://meet.jit.si/{{ owner }}-{{ booker }}-{{ date }}-{{ random }}"
    REMINDER_MINUTES: int | None = 15
    EVENT_TITLE: str = DEFAULT_EVENT_TITLE
    EVENT_DESCRIPTION: str = DEFAULT_EVENT_DESCRIPTION
    EMAIL_BODY_BOOKER: str = DEFAULT_EMAIL_BODY_BOOKER
    EMAIL_BODY_OWNER: str = DEFAULT_EMAIL_BODY_OWNER

    # SMTP settings (SYSTEM_EMAIL is the sender and the ORGANIZER of invitations)
    SYSTEM_EMAIL: str = ""
    SYSTEM_NAME: str = ""  # Name shown for SYSTEM_EMAIL (From and ORGANIZER); empty: OWNER_NAME
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_STARTTLS: bool = True

    # Rate limit for all POST requests (per client IP)
    RATE_LIMIT_POST: str = "10 per hour"

    # Profiles: slug -> overrides of PROFILE_KEYS. Everything else is inherited from the top level.
    PROFILES: ClassVar[dict[str, dict[str, Any]]] = {}
    DEFAULT_PROFILE: str = ""  # Slug of the profile that is also served at "/"
    ROOT_PAGE: str = "list"  # What "/" shows without a default profile: "list" or "placeholder"

    @classmethod
    def validate_config_schema(cls, cfg: dict, schema: dict) -> None:
        """Validate the config against a JSON schema."""
        try:
            validate(instance=cfg, schema=schema, format_checker=FormatChecker())
        except ValidationError as e:
            logging.critical("Config validation failed: %s", e.message)
            raise ValueError(e) from None
        logging.debug("Config validated successfully against schema.")

    @classmethod
    def validate_semantics(cls, cfg: dict) -> None:
        """Validate what a JSON schema cannot: time zone, window order, Jinja2 syntax.

        Raises:
            ValueError: with a readable message if a value is invalid
        """
        tz = cfg.get("TIMEZONE", cls.TIMEZONE)
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            msg = f"TIMEZONE '{tz}' is not a known IANA time zone (e.g. Europe/Berlin)"
            raise ValueError(msg) from None

        for day, windows in cfg.get("WORKING_HOURS", {}).items():
            for window in windows:
                start, end = window.split("-")
                if start >= end:
                    msg = f"WORKING_HOURS {day}: '{window}' must end after it starts"
                    raise ValueError(msg)

        cls._validate_templates(cfg)

    @classmethod
    def validate_profiles(cls, cfg: dict) -> None:
        """Check the profiles of a loaded config: semantics of each, default, required settings.

        Raises:
            ValueError: with a readable message if something is invalid or missing
        """
        profiles = cfg.get("PROFILES", {})
        default = cfg.get("DEFAULT_PROFILE", cls.DEFAULT_PROFILE)
        if default and default not in profiles:
            msg = f"DEFAULT_PROFILE '{default}' is not defined in PROFILES"
            raise ValueError(msg)

        for slug, overrides in profiles.items():
            try:
                cls.validate_semantics(overrides)
            except ValueError as e:  # noqa: PERF203 (only on the way out: names the profile)
                msg = f"Profile '{slug}': {e}"
                raise ValueError(msg) from None
        for slug in profiles or [None]:
            merged = profile_config(cfg, slug)
            for key in REQUIRED_PROFILE_KEYS:
                if not merged.get(key):
                    where = f"profile '{slug}'" if slug else "the config"
                    msg = f"{key} must be set in {where}, or at the top level"
                    raise ValueError(msg)

    @classmethod
    def _validate_templates(cls, cfg: dict) -> None:
        """Check the Jinja2 templates: syntax, and that MEETING_URL gives a usable URL.

        Raises:
            ValueError: with a readable message if a template is invalid
        """
        # Sandboxed: templates come from the config, but there is no reason to trust them blindly
        env = SandboxedEnvironment()
        for key in ("EVENT_TITLE", "EVENT_DESCRIPTION", "EMAIL_BODY_BOOKER", "EMAIL_BODY_OWNER"):
            if key in cfg:
                try:
                    env.from_string(cfg[key])
                except TemplateSyntaxError as e:
                    msg = f"{key} is not a valid Jinja2 template: {e}"
                    raise ValueError(msg) from None

        if "MEETING_URL" in cfg:
            try:
                sample = env.from_string(cfg["MEETING_URL"]).render(**MEETING_URL_SAMPLE)
            except TemplateSyntaxError as e:
                msg = f"MEETING_URL is not a valid Jinja2 template: {e}"
                raise ValueError(msg) from None
            if not re.fullmatch(r"https?://[^\s\x00-\x1f\x7f]+", sample):
                msg = f"MEETING_URL must result in a URL without spaces, but gives '{sample}'"
                raise ValueError(msg)

    @classmethod
    def load_from_yaml(cls, yaml_path: str | Path) -> dict[str, Any]:
        """Load and validate configuration from YAML file.

        Args:
            yaml_path: Path to YAML configuration file

        Returns:
            Dictionary with configuration values
        """
        logging.debug("Loading configuration from YAML file: %s", yaml_path)
        try:
            with Path(yaml_path).open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logging.critical("Configuration file not found: %s", yaml_path)
            raise
        except yaml.YAMLError as e:
            logging.critical("Error parsing YAML configuration file: %s", e)
            raise
        cls.validate_config_schema(data, CONFIG_SCHEMA)
        cls.validate_semantics(data)
        cls.validate_profiles(data)
        return data

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> "AppConfig":
        """Create Config instance from YAML file, overriding class defaults.

        Args:
            yaml_path (str | Path): Path to YAML configuration file

        Returns:
            Config instance with merged configuration
        """
        config = cls()
        for key, value in cls.load_from_yaml(yaml_path).items():
            setattr(config, key, value)
        return config
