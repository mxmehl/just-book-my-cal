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


def test_default_config_path_is_in_the_user_config_dir_not_relative():
    from pathlib import Path

    from justbookmycal.config import default_config_path

    path = Path(default_config_path())
    assert path.is_absolute()
    assert path.parts[-2:] == ("justbookmycal", "config.yaml")


def test_gunicorn_wrapper_passes_the_default_config_path(command):
    from justbookmycal.config import default_config_path

    cmd = command()
    assert f"CONFIG_FILE={default_config_path()}" in cmd
    assert "CONFIG_FILE=config.yaml" not in cmd


def test_cli_default_config_path(monkeypatch):
    from justbookmycal import app as app_module
    from justbookmycal.config import default_config_path

    seen = {}
    monkeypatch.setattr(
        app_module,
        "create_app_wrapper",
        lambda app_config_path, debug: (
            seen.update(path=app_config_path) or (_ for _ in ()).throw(SystemExit)
        ),
    )
    monkeypatch.setattr(sys, "argv", ["justbookmycal-cli", "--db", "check"])
    with pytest.raises(SystemExit):
        app_module.main()
    assert seen["path"] == default_config_path()


def test_missing_config_names_the_full_path(monkeypatch, caplog):
    monkeypatch.setenv("CONFIG_FILE", "does-not-exist.yaml")
    assert wsgi.main() is None
    from pathlib import Path

    assert str(Path("does-not-exist.yaml").resolve()) in caplog.text
