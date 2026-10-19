# SPDX-FileCopyrightText: 2026 Max Mehl <https://mehl.mx>
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the command that starts gunicorn."""

import subprocess
import sys

import pytest

from justbookmycal import wsgi


@pytest.fixture(name="command")
def fixture_command(monkeypatch):
    """Run wsgi.gunicorn() with the given arguments and return the gunicorn command line."""

    def run(*args: str) -> list[str]:
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, check: calls.append(cmd))
        monkeypatch.setattr(sys, "argv", ["justbookmycal", *args])
        wsgi.gunicorn()
        [cmd] = calls
        return cmd

    return run


def test_default_uses_the_bundled_config_and_its_bind_address(command):
    cmd = command("--app-config", "/etc/jbmc/config.yaml")
    assert "-b" not in cmd  # the "bind" of gunicorn.conf.py applies: 0.0.0.0
    assert cmd[cmd.index("-c") + 1].endswith("gunicorn.conf.py")
    assert "CONFIG_FILE=/etc/jbmc/config.yaml" in cmd
    assert "justbookmycal.wsgi:main()" in cmd


def test_bind_option_is_passed_to_gunicorn(command):
    cmd = command("--bind", "127.0.0.1:5262")
    assert cmd[cmd.index("-b") + 1] == "127.0.0.1:5262"
    # Before the application argument, as gunicorn options should be
    assert cmd.index("-b") < cmd.index("justbookmycal.wsgi:main()")


def test_bundled_config_listens_on_all_interfaces():
    from pathlib import Path

    from justbookmycal import __file__ as package_init

    config = (Path(package_init).parent / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert 'bind = "0.0.0.0:' in config
