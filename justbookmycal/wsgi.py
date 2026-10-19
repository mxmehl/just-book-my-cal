# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""WSGI entry point for production servers like gunicorn."""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask

from . import __version__
from .app import create_app_wrapper


def main() -> Flask | None:
    """Entrypoint for WSGI servers. Reads CONFIG_FILE and DEBUG from the environment.

    Returns:
        The Flask app instance, or None if the configuration file is missing
    """
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    config_path = os.environ.get("CONFIG_FILE")
    if config_path is None:
        logging.critical("CONFIG_FILE environment variable is not set")
        return None
    if not Path(config_path).exists():
        logging.critical("Configuration file %s does not exist", config_path)
        return None
    config_path = str(Path(config_path).resolve())
    logging.info("Using configuration file: %s", config_path)

    return create_app_wrapper(app_config_path=config_path, debug=debug)


def gunicorn() -> None:
    """Run Gunicorn server with the specified configuration."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-c",
        "--app-config",
        type=str,
        help="Path to YAML configuration file",
        default="config.yaml",
    )
    parser.add_argument(
        "-gc",
        "--gunicorn-config",
        type=str,
        help="Path to Gunicorn configuration file. Defaults to the one shipped with the package.",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Run in debug mode (may leak sensitive information)"
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = parser.parse_args()

    gunicorn_config_path = args.gunicorn_config or str(Path(__file__).parent / "gunicorn.conf.py")

    subprocess.run(  # noqa: S603
        [
            str(Path(sys.executable).parent / "gunicorn"),
            "-c",
            gunicorn_config_path,
            "justbookmycal.wsgi:main()",
            "-e",
            f"CONFIG_FILE={args.app_config}",
            "-e",
            f"DEBUG={args.debug}",
        ],
        check=True,
    )
