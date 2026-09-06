"""Tests for the EIS D-Bus error contract in EISClient._setup.

Ported from upstream isac322/kwin-mcp#42: the dbus calls in ``_setup``
(get_object → Interface → connectToEIS) were unprotected, and
``dbus.DBusException`` is not a ``RuntimeError`` — so a KWin without a usable
EIS interface crashed the whole ``session_start``/``session_connect`` instead
of degrading to "no input backend" (core.py catches only RuntimeError).
"""

from __future__ import annotations

from typing import Any

import dbus
import pytest

import kwin_mcp.input as input_module
from kwin_mcp.input import EISClient


class _FailingBus:
    """BusConnection stub whose get_object raises the anticipated D-Bus error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_object(self, *args: Any, **kwargs: Any) -> None:
        raise self._exc


def _client_with_bus(bus: Any) -> EISClient:
    """An EISClient that skipped __init__ (no D-Bus connection, no libei load)."""
    client = EISClient.__new__(EISClient)
    client._bus = bus
    return client


def test_setup_translates_dbus_exception_to_runtime_error() -> None:
    """dbus.DBusException from get_object → RuntimeError naming the EIS interface."""
    exc = dbus.DBusException("org.freedesktop.DBus.Error.ServiceUnknown: no org.kde.KWin")
    client = _client_with_bus(_FailingBus(exc))
    with pytest.raises(RuntimeError, match="KWin EIS interface unavailable") as caught:
        client._setup()
    assert "ServiceUnknown" in str(caught.value)
    # Error chaining preserved for diagnostics.
    assert isinstance(caught.value.__cause__, dbus.DBusException)


def test_setup_translates_connect_failure_from_interface_proxy(monkeypatch) -> None:
    """A DBusException surfacing later (Interface/connectToEIS stage) is also
    translated: the whole dbus block is covered, not just get_object."""

    class _ThrowingIface:
        def __init__(self, *args: Any) -> None:
            pass

        # Mirrors the real D-Bus method name (camelCase per the KWin interface).
        def connectToEIS(self, *args: Any) -> None:  # noqa: N802
            raise dbus.DBusException("org.kde.KWin.EIS.RemoteDesktop: not supported")

    monkeypatch.setattr(input_module.dbus, "Interface", _ThrowingIface)
    client = _client_with_bus(object())

    class _OkBus:
        def get_object(self, *args: Any, **kwargs: Any) -> object:
            return object()

    client = _client_with_bus(_OkBus())
    with pytest.raises(RuntimeError, match="KWin EIS interface unavailable") as caught:
        client._setup()
    assert "not supported" in str(caught.value)
