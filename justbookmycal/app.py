# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Flask app and CLI for just-book-my-cal."""

import argparse
import logging
from datetime import timedelta
from logging.config import dictConfig
from pathlib import Path

import click
from flask import Flask, Response, render_template, request
from flask_migrate import Migrate, check, downgrade, migrate, upgrade
from flask_wtf import CSRFProtect
from sqlalchemy import inspect
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, availability
from .availability import FeedError
from .config import AppConfig, default_config_path, default_database_uri, profile_config
from .extensions import db, limiter
from .views import bp

DEFAULT_PORT = 5262


def configure_logging(debug: bool) -> None:
    """Configure logging."""
    dictConfig(
        {
            "version": 1,
            "formatters": {
                "default": {"format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"}
            },
            "handlers": {
                "wsgi": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://flask.logging.wsgi_errors_stream",
                    "formatter": "default",
                }
            },
            "root": {"level": "DEBUG" if debug else "INFO", "handlers": ["wsgi"]},
        }
    )


def _register_error_handlers(app: Flask) -> None:
    """Render all errors with the friendly error page."""

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException) -> tuple[str, int]:
        return render_template("error.html", status=e.code, message=e.description), e.code or 500

    @app.errorhandler(FeedError)
    def feed_error(e: FeedError) -> tuple[str, int]:
        logging.error("%s", e)
        message = (
            "The calendar is temporarily unavailable, so free times cannot be shown. "
            "Please try again in a few minutes."
        )
        return render_template("error.html", status=503, message=message), 503


def _register_cli(app: Flask) -> None:
    """Commands to check the configuration against the real feeds: `flask busy`, `flask slots`."""

    def settings(profile: str | None) -> dict:
        """The settings to check: of the given profile, else of the default one (or top level)."""
        cfg = app.config
        if profile and profile not in cfg["PROFILES"]:
            known = ", ".join(cfg["PROFILES"]) or "none configured"
            msg = f"unknown profile '{profile}' (known: {known})"
            raise click.BadParameter(msg, param_hint="--profile")
        if not profile and cfg["PROFILES"] and not cfg["DEFAULT_PROFILE"]:
            msg = f"there is no default profile, choose one of: {', '.join(cfg['PROFILES'])}"
            raise click.BadParameter(msg, param_hint="--profile")
        return profile_config(cfg, profile or cfg["DEFAULT_PROFILE"] or None)

    profile_option = click.option(
        "--profile", help="Profile to check (default: DEFAULT_PROFILE, or the top-level settings)"
    )

    @app.cli.command("busy")
    @click.option("--days", default=7, show_default=True, help="How many days to look ahead")
    @profile_option
    def busy_command(days: int, profile: str | None) -> None:
        """Print the busy blocks found in the calendar feeds."""
        cfg = settings(profile)
        now = availability.utcnow()
        tz = availability.Rules.from_config(cfg).tz
        for start, end, uid in sorted(
            availability.gather_busy(cfg, now, now + timedelta(days=days))
        ):
            fmt = "%a %Y-%m-%d %H:%M"
            click.echo(f"{start.astimezone(tz):{fmt}} - {end.astimezone(tz):%H:%M}  {uid}")

    @app.cli.command("slots")
    @click.option("--duration", default=30, show_default=True, help="Meeting length in minutes")
    @profile_option
    def slots_command(duration: int, profile: str | None) -> None:
        """Print the bookable slots for a meeting length."""
        cfg = settings(profile)
        tz = availability.Rules.from_config(cfg).tz
        for slot in availability.free_slots(cfg, duration, availability.utcnow()):
            click.echo(f"{slot.astimezone(tz):%a %Y-%m-%d %H:%M %Z}")


def create_app(
    config_overrides: dict | None = None,
    yaml_config_path: str | None = None,
    debug: bool = False,
) -> Flask:
    """Create Flask app.

    Args:
        config_overrides (dict): optional dict to update app.config before DB init (e.g. for tests)
        yaml_config_path (str): optional path to YAML configuration file
        debug (bool): if True, enable debug mode

    Returns:
        Flask: the Flask application
    """
    app = Flask(__name__)

    appconfig = AppConfig.from_yaml(yaml_config_path) if yaml_config_path else AppConfig()
    app.config.from_object(appconfig)
    app.debug = debug
    app.config["DEBUG"] = debug
    if config_overrides:
        app.config.update(config_overrides)

    # Fail fast if SECRET_KEY is not set: an empty key makes sessions and CSRF trivially forgeable
    if not app.config.get("TESTING") and not app.config.get("SECRET_KEY"):
        msg = "SECRET_KEY must be set and non-empty in your config.yaml"
        raise ValueError(msg)

    # Database. Default: next to the config file in the user config directory, as for the config
    if "SQLALCHEMY_DATABASE_URI" not in app.config:
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["DATABASE_URI"] or default_database_uri()
    logging.info("Using database at %s", app.config["SQLALCHEMY_DATABASE_URI"])
    db.init_app(app)
    Migrate(app=app, db=db, directory=str(Path(__file__).parent.resolve() / "migrations"))

    # Trust headers from the reverse proxy, so rate limiting sees the real client IP
    if hops := app.config["TRUSTED_PROXIES"]:
        app.wsgi_app = ProxyFix(  # type: ignore[ty:invalid-assignment]
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_prefix=hops
        )

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=not debug,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    CSRFProtect(app)

    app.register_blueprint(bp)
    _register_error_handlers(app)
    _register_cli(app)

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # The URLs of manage/admin pages are secrets: never leak them via Referer
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        # Slots change constantly and pages contain personal data: only static files may be cached
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.config.setdefault("RATELIMIT_STORAGE_URI", "memory://")
    limiter.init_app(app)
    if app.config["RATELIMIT_STORAGE_URI"] == "memory://" and not app.debug:
        logging.warning(
            "Rate limiting uses in-memory storage. Limits are per gunicorn worker process."
        )
    return app


def create_app_wrapper(app_config_path: str, debug: bool) -> Flask:
    """Create the app from arguments. Used by the CLI and by WSGI (gunicorn)."""
    configure_logging(debug)
    return create_app(yaml_config_path=app_config_path, debug=debug)


def main() -> None:
    """Run the app."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-H", "--host", type=str, help="Host of Flask app", default="127.0.0.1")
    parser.add_argument("-p", "--port", type=int, help="Port of Flask app", default=DEFAULT_PORT)
    parser.add_argument(
        "-c",
        "--app-config",
        type=str,
        help="Path to YAML configuration file",
        default=default_config_path(),
    )
    parser.add_argument(
        "--db",
        choices=["check", "upgrade", "downgrade"],
        help="Database commands. Run `--db upgrade` once before the first start",
    )
    parser.add_argument(
        "--db-migrate", type=str, help="Create a DB migration with the given message"
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument(
        "--debug", action="store_true", help="Run in debug mode (may leak sensitive information)"
    )
    args = parser.parse_args()

    one_off = bool(args.db or args.db_migrate)
    app = create_app_wrapper(app_config_path=args.app_config, debug=args.debug)

    if one_off:
        with app.app_context():
            if args.db_migrate:
                migrate(message=args.db_migrate)
            elif args.db == "check":
                check()
            elif args.db == "upgrade":
                upgrade()
            elif args.db == "downgrade":
                downgrade()
        return

    with app.app_context():
        if not inspect(db.engine).has_table("booking"):
            logging.critical("The database is not initialized. Run with `--db upgrade` first.")
            return

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
