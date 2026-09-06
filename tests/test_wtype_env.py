"""Tests for wtype environment construction in InputBackend.keyboard_type_unicode.

Regression for the H-1 finding (wingman #227): the wtype branch built its
environment from ``os.environ`` plus the session D-Bus address only, so wtype
had no ``WAYLAND_DISPLAY`` and failed with "Wayland connection failed" inside
an isolated virtual session (rc=1), making the wl-copy fallback unreachable.

The wtype env must be provided by the caller (AutomationEngine builds it via
_session_env()) and must contain the session's WAYLAND_DISPLAY and
XDG_RUNTIME_DIR so that wtype connects to the isolated compositor.
"""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import MagicMock

from kwin_mcp.input import InputBackend

_SESSION_ENV = {
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake-session-bus",
    "WAYLAND_DISPLAY": "wayland-mcp-test-42",
    "XDG_RUNTIME_DIR": "/tmp/fake-runtime",
    "HOME": "/tmp/fake-home",
}


def test_wtype_receives_session_env(monkeypatch) -> None:
    """wtype is invoked with the caller-provided env, not the host environ."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0-host")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["env"] = env
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(shutil, "which", lambda name: name == "wtype")
    monkeypatch.setattr(subprocess, "run", fake_run)

    backend = InputBackend.__new__(InputBackend)  # skip EIS connection
    ok = backend.keyboard_type_unicode("привет", env=_SESSION_ENV)

    assert ok is True
    run_env = captured["env"]
    assert isinstance(run_env, dict)
    # Session-specific values must win over any host-inherited values.
    assert run_env["WAYLAND_DISPLAY"] == "wayland-mcp-test-42"
    assert run_env["XDG_RUNTIME_DIR"] == "/tmp/fake-runtime"
    assert run_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/fake-session-bus"
    assert captured["cmd"] == ["wtype", "--", "привет"]


def test_wtype_failure_falls_back_to_clipboard(monkeypatch) -> None:
    """A wtype failure (e.g. missing virtual-keyboard protocol) is retried via wl-copy."""
    monkeypatch.setattr(shutil, "which", lambda name: name in ("wtype", "wl-copy"))

    def fake_run(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 1  # wtype fails ("Wayland connection failed")
        return result

    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], env: dict[str, str], **kwargs: object) -> MagicMock:
        captured["cmd"] = cmd
        captured["env"] = env
        proc = MagicMock()
        proc.poll.return_value = None  # wl-copy is running
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    backend = InputBackend.__new__(InputBackend)
    backend.keyboard_key = MagicMock()  # type: ignore[method-assign]
    ok = backend.keyboard_type_unicode("héllo", env=_SESSION_ENV)

    assert ok is True
    assert captured["cmd"] == ["wl-copy", "--", "héllo"]
    assert captured["env"] == _SESSION_ENV  # fallback reuses the session env
    # Paste keys: Konsole binds paste to Ctrl+Shift+V, most apps Ctrl+V; the
    # trailing Returns commit the paste in shells (the second one satisfies
    # the ^V quoted-insert state the unbound Ctrl+V leaves in zsh).
    sent = [call.args[0] for call in backend.keyboard_key.call_args_list]
    assert sent == ["ctrl+shift+v", "ctrl+v", "return", "return"]


def test_no_tools_returns_false(monkeypatch) -> None:
    """Neither wtype nor wl-copy available → returns False, nothing spawned."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", MagicMock())
    monkeypatch.setattr(subprocess, "Popen", MagicMock())

    backend = InputBackend.__new__(InputBackend)
    ok = backend.keyboard_type_unicode("test", env=_SESSION_ENV)

    assert ok is False
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]
