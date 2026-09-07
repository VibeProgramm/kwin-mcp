"""Input injection via KWin's EIS (Emulated Input Server) D-Bus interface.

Uses KWin's private org.kde.KWin.EIS.RemoteDesktop D-Bus interface to get
a direct EIS file descriptor, then uses libei to inject mouse/keyboard
events into the isolated kwin_wayland --virtual session.

This bypasses the XDG RemoteDesktop portal (which requires user authorization)
and communicates directly with the KWin compositor.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import os
import select
import shutil
import subprocess
import sys
import time
from enum import Enum

import dbus
import dbus.bus
from dbus.mainloop.glib import DBusGMainLoop

from kwin_mcp.errors import ToolError, tool_error

logger = logging.getLogger(__name__)

# KWIN_MCP_DEBUG_EI=1 enables libei/EIS diagnostics: the kwin_mcp.input logger
# is raised to DEBUG level and given a stderr handler, so device
# ADDED/REMOVED/RESUMED/PAUSED events, stalls and reconnects become visible
# (documented in the README "Debugging" section).
_DEBUG_EI = os.environ.get("KWIN_MCP_DEBUG_EI") == "1"
if _DEBUG_EI:
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        _stderr_handler = logging.StreamHandler(sys.stderr)
        _stderr_handler.setFormatter(logging.Formatter("[EI] %(message)s"))
        logger.addHandler(_stderr_handler)
        logger.propagate = False


def _ei_debug(msg: str) -> None:
    """Log an EI debug line (visible when KWIN_MCP_DEBUG_EI=1 is set)."""
    logger.debug(msg)


class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


# Linux input event codes for mouse buttons
_BTN_CODES: dict[MouseButton, int] = {
    MouseButton.LEFT: 0x110,  # BTN_LEFT
    MouseButton.RIGHT: 0x111,  # BTN_RIGHT
    MouseButton.MIDDLE: 0x112,  # BTN_MIDDLE
}

# Linux evdev keycodes for special keys
_EVDEV_KEY_MAP: dict[str, int] = {
    "return": 28,
    "enter": 28,
    "tab": 15,
    "escape": 1,
    "backspace": 14,
    "delete": 111,
    "space": 57,
    "up": 103,
    "down": 108,
    "left": 105,
    "right": 106,
    "home": 102,
    "end": 107,
    "page_up": 104,
    "pageup": 104,
    "page_down": 109,
    "pagedown": 109,
    "insert": 110,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "print": 99,
    "scroll_lock": 70,
    "pause": 119,
    "caps_lock": 58,
    "num_lock": 69,
    "menu": 127,
}

# Modifier evdev keycodes
_MODIFIER_KEYS: dict[str, int] = {
    "shift": 42,  # KEY_LEFTSHIFT
    "ctrl": 29,  # KEY_LEFTCTRL
    "control": 29,
    "alt": 56,  # KEY_LEFTALT
    "super": 125,  # KEY_LEFTMETA
    "meta": 125,
}

# Character to evdev keycode mapping (US QWERTY layout)
_CHAR_KEY_MAP: dict[str, tuple[int, bool]] = {}

# Build character → (keycode, needs_shift) mapping
_QWERTY_ROWS = [
    # (normal_chars, shifted_chars, keycodes)
    ("`1234567890-=", "~!@#$%^&*()_+", [41, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
    ("qwertyuiop[]\\", "QWERTYUIOP{}|", [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 43]),
    ("asdfghjkl;'", 'ASDFGHJKL:"', [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40]),
    ("zxcvbnm,./", "ZXCVBNM<>?", [44, 45, 46, 47, 48, 49, 50, 51, 52, 53]),
]

for normal, shifted, codes in _QWERTY_ROWS:
    for char, code in zip(normal, codes, strict=True):
        _CHAR_KEY_MAP[char] = (code, False)
    for char, code in zip(shifted, codes, strict=True):
        _CHAR_KEY_MAP[char] = (code, True)

_CHAR_KEY_MAP[" "] = (57, False)  # space
_CHAR_KEY_MAP["\t"] = (15, False)  # tab
_CHAR_KEY_MAP["\n"] = (28, False)  # enter

# Button states
_PRESSED = 1
_RELEASED = 0

# EI device capabilities (bitfield)
_EI_CAP_POINTER = 1 << 0
_EI_CAP_POINTER_ABSOLUTE = 1 << 1
_EI_CAP_KEYBOARD = 1 << 2
_EI_CAP_TOUCH = 1 << 3
_EI_CAP_SCROLL = 1 << 4
_EI_CAP_BUTTON = 1 << 5
_EI_CAP_TEXT = 1 << 6  # libei >= 1.6: keysym/UTF-8 input resolved server-side

# EI event types (enum ei_event_type, libei.h 1.6.0; CONNECT is 1 and the
# values increment in declaration order up to KEYBOARD_MODIFIERS = 9;
# EI_EVENT_PONG is 90). Kept as literals — the module-level cross-check test
# (test_libei_constants.py) parses the installed libei.h and would catch a
# drift against the real header.
_EI_EVENT_CONNECT = 1
_EI_EVENT_DISCONNECT = 2
_EI_EVENT_SEAT_ADDED = 3
_EI_EVENT_SEAT_REMOVED = 4
_EI_EVENT_DEVICE_ADDED = 5
_EI_EVENT_DEVICE_REMOVED = 6
_EI_EVENT_DEVICE_PAUSED = 7
_EI_EVENT_DEVICE_RESUMED = 8
_EI_EVENT_KEYBOARD_MODIFIERS = 9

# Scroll axis values (in libei, scroll is in pixels)
_SCROLL_STEP_PIXELS = 15.0

# Pre-reconnect stall wait (wingman #231, #235): a PAUSED stall without a
# RESUMED leads into a reconnect anyway, so waiting longer before it only
# burns latency in KWin's pause cycle (handshake → resume → pause, the fresh
# connection lives under a second). 0.5s still catches an ordinary late
# resume without turning every transient pause into a reconnect.
_STALL_READY_TIMEOUT_S = 0.5

# Post-reconnect bounded retry loop (wingman #235): KWin may pause its EIS
# devices right after EVERY handshake (the fresh connection lives under a
# second, then the cycle repeats), so a single reconnect + re-check (the
# #228 shape) loses against the cycle. The gate now retries the reconnect a
# bounded number of times, each attempt getting one short re-check window;
# when the budget is exhausted the injection fails loudly with the attempt
# count instead of being silently dropped into a paused device.
#
# Budget: 3 attempts, each getting one short re-check window; when the
# budget is exhausted the injection fails loudly with the attempt count
# instead of being silently dropped into a paused device. A reconnect that
# RAISES (D-Bus/libei/handshake error) still aborts immediately — retrying
# a hard failure adds only latency, the error is already honest (#229/F1
# keep the state clean for the next call).
#
# In the live cycle each handshake resolves in hundreds of milliseconds,
# so the loop ends in ~3-4s as before — but the attempt count alone does
# not bound one call: a wedged handshake can sit in _negotiate_devices up
# to its 5s deadline on EVERY attempt (≈17s worst case). The loop therefore
# additionally stops on the wall-clock budget below (issue #16, F7).
_RECONNECT_ATTEMPTS = 3

# Reconnect-attempt wall budget (issue #16, F7; named issue #18, R2):
# bounds the retry loop's wall time alongside the attempt count, checked
# between attempts — NOT a call-wide deadline for one
# ``_ensure_devices_ready`` call. An in-flight ``_reconnect`` →
# ``_negotiate_devices(timeout=5.0)`` already past the check runs to its
# own deadline, so the honest worst case per call stays: the stall wait
# (0.5s) + this budget (4s) + one overrunning handshake (<=5s) + its
# re-check (0.5s) ≈ 10s; in the live cycle (fast handshakes) the loop
# still ends in ~3-4s.
_RECONNECT_RETRY_BUDGET_S = 4.0

# Post-reconnect re-check window for extra required device slots (text/touch)
# and for the fresh handshake's devices in general (#235 retry loop): a
# fresh handshake only guarantees pointer + keyboard reaching the READY
# state; a required extra slot gets this much time per attempt to reach
# RESUMED before the attempt is judged failed and the loop continues
# (wingman #228, #235).
_POST_RECONNECT_READY_TIMEOUT_S = 0.5

# Slot attributes holding the negotiated EIS device handles. The tuple
# travels together wherever devices are dropped or released, so it lives in
# one place instead of being repeated at every teardown site.
_DEVICE_ATTRS: tuple[str, str, str, str] = (
    "_pointer",
    "_keyboard",
    "_touch_device",
    "_text_device",
)

# XKB keysyms for control characters (XK_Return, XK_Tab)
_XKB_KEYSYM_RETURN = 0xFF0D
_XKB_KEYSYM_TAB = 0xFF09

# XKB keysyms for named special keys (from xkbcommon-keysyms.h). The server
# resolves these through its own keymap, so no client-side keymap knowledge
# is required.
_KEYSYM_NAME_MAP: dict[str, int] = {
    "return": _XKB_KEYSYM_RETURN,
    "enter": _XKB_KEYSYM_RETURN,
    "tab": _XKB_KEYSYM_TAB,
    "escape": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "space": 0x20,
    "up": 0xFF52,
    "down": 0xFF54,
    "left": 0xFF51,
    "right": 0xFF53,
    "home": 0xFF50,
    "end": 0xFF57,
    "page_up": 0xFF55,
    "pageup": 0xFF55,
    "page_down": 0xFF56,
    "pagedown": 0xFF56,
    "insert": 0xFF63,
    "print": 0xFF61,
    "scroll_lock": 0xFF14,
    "pause": 0xFF13,
    "caps_lock": 0xFFE5,
    "num_lock": 0xFF7F,
    "menu": 0xFF67,
    "f1": 0xFFBE,
    "f2": 0xFFBF,
    "f3": 0xFFC0,
    "f4": 0xFFC1,
    "f5": 0xFFC2,
    "f6": 0xFFC3,
    "f7": 0xFFC4,
    "f8": 0xFFC5,
    "f9": 0xFFC6,
    "f10": 0xFFC7,
    "f11": 0xFFC8,
    "f12": 0xFFC9,
}

# Printable ASCII → XKB keysym. XKB assigns Latin-1 keysyms to the same
# codepoints as ASCII, so this table is a straight pass-through for 0x20-0x7E.
_ASCII_TO_KEYSYM: dict[str, int] = {chr(c): c for c in range(0x20, 0x7F)}
_ASCII_TO_KEYSYM["\n"] = _XKB_KEYSYM_RETURN
_ASCII_TO_KEYSYM["\t"] = _XKB_KEYSYM_TAB


def ascii_char_to_keysym(char: str) -> int | None:
    """Map a single character to its XKB keysym, or None if not mappable."""
    return _ASCII_TO_KEYSYM.get(char)


def key_name_to_keysym(name: str) -> int | None:
    """Map a key name (e.g. 'Return', 'F5') to its XKB keysym, or None."""
    return _KEYSYM_NAME_MAP.get(name.lower())


def _load_libei() -> ctypes.CDLL:
    """Load libei shared library and set up function prototypes."""
    lib = ctypes.CDLL("libei.so.1")

    # Context management
    lib.ei_new_sender.restype = ctypes.c_void_p
    lib.ei_new_sender.argtypes = [ctypes.c_void_p]
    lib.ei_configure_name.restype = None
    lib.ei_configure_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.ei_setup_backend_fd.restype = ctypes.c_int
    lib.ei_setup_backend_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
    # ei_dispatch is VOID (libei.h): it returns nothing. Errors are handled
    # internally (the connection is ei_disconnect()ed) and surface to the
    # caller only as a synthesized EI_EVENT_DISCONNECT event on the next
    # dispatch + drain — never as a return code.
    lib.ei_dispatch.restype = None
    lib.ei_dispatch.argtypes = [ctypes.c_void_p]
    lib.ei_get_event.restype = ctypes.c_void_p
    lib.ei_get_event.argtypes = [ctypes.c_void_p]
    lib.ei_event_get_type.restype = ctypes.c_int
    lib.ei_event_get_type.argtypes = [ctypes.c_void_p]
    lib.ei_event_unref.restype = ctypes.c_void_p
    lib.ei_event_unref.argtypes = [ctypes.c_void_p]
    lib.ei_unref.restype = ctypes.c_void_p
    lib.ei_unref.argtypes = [ctypes.c_void_p]
    lib.ei_get_fd.restype = ctypes.c_int
    lib.ei_get_fd.argtypes = [ctypes.c_void_p]

    # Seat functions
    lib.ei_event_get_seat.restype = ctypes.c_void_p
    lib.ei_event_get_seat.argtypes = [ctypes.c_void_p]
    lib.ei_seat_has_capability.restype = ctypes.c_int
    lib.ei_seat_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.ei_seat_ref.restype = ctypes.c_void_p
    lib.ei_seat_ref.argtypes = [ctypes.c_void_p]
    lib.ei_seat_bind_capabilities.restype = None
    lib.ei_seat_bind_capabilities.argtypes = [ctypes.c_void_p]  # variadic: fixed param only

    # Device functions
    lib.ei_event_get_device.restype = ctypes.c_void_p
    lib.ei_event_get_device.argtypes = [ctypes.c_void_p]
    lib.ei_device_get_name.restype = ctypes.c_char_p
    lib.ei_device_get_name.argtypes = [ctypes.c_void_p]
    lib.ei_device_has_capability.restype = ctypes.c_int
    lib.ei_device_has_capability.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.ei_device_ref.restype = ctypes.c_void_p
    lib.ei_device_ref.argtypes = [ctypes.c_void_p]
    lib.ei_device_unref.restype = ctypes.c_void_p
    lib.ei_device_unref.argtypes = [ctypes.c_void_p]

    # Input injection
    lib.ei_device_pointer_motion.restype = None
    lib.ei_device_pointer_motion.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_device_pointer_motion_absolute.restype = None
    lib.ei_device_pointer_motion_absolute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        ctypes.c_double,
    ]
    lib.ei_device_button_button.restype = None
    lib.ei_device_button_button.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    lib.ei_device_scroll_delta.restype = None
    lib.ei_device_scroll_delta.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_device_scroll_discrete.restype = None
    lib.ei_device_scroll_discrete.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]
    lib.ei_device_scroll_stop.restype = None
    lib.ei_device_scroll_stop.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.ei_device_keyboard_key.restype = None
    lib.ei_device_keyboard_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    lib.ei_device_frame.restype = None
    lib.ei_device_frame.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    lib.ei_device_start_emulating.restype = None
    lib.ei_device_start_emulating.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.ei_device_stop_emulating.restype = None
    lib.ei_device_stop_emulating.argtypes = [ctypes.c_void_p]

    # Text input (libei >= 1.6). Events are resolved by the server: keysyms go
    # through the server's keymap (EisDevice::sendKeySym in KWin), UTF-8 text
    # goes through the input method. These symbols are missing on older libei;
    # guard with hasattr so the module still loads there.
    if hasattr(lib, "ei_device_text_keysym"):
        lib.ei_device_text_keysym.restype = None
        lib.ei_device_text_keysym.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
    if hasattr(lib, "ei_device_text_utf8"):
        lib.ei_device_text_utf8.restype = None
        lib.ei_device_text_utf8.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    # Touch functions
    lib.ei_device_touch_new.restype = ctypes.c_void_p
    lib.ei_device_touch_new.argtypes = [ctypes.c_void_p]
    lib.ei_touch_down.restype = None
    lib.ei_touch_down.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_touch_motion.restype = None
    lib.ei_touch_motion.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
    lib.ei_touch_up.restype = None
    lib.ei_touch_up.argtypes = [ctypes.c_void_p]
    lib.ei_touch_unref.restype = ctypes.c_void_p
    lib.ei_touch_unref.argtypes = [ctypes.c_void_p]

    return lib


# Module-level libei instance, loaded lazily on first use. Loading eagerly at
# import time makes importing this module fail on systems without libei
# installed (e.g. the CI test job, which only exercises module structure),
# even though no EIS input path is touched there.
_libei: ctypes.CDLL | None = None


def _get_libei() -> ctypes.CDLL:
    """Return the process-wide libei handle, loading it on first call."""
    global _libei
    if _libei is None:
        _libei = _load_libei()
    return _libei


class EISClient:
    """Low-level EIS client using KWin's direct D-Bus interface + libei.

    Connects to KWin's org.kde.KWin.EIS.RemoteDesktop D-Bus interface
    to get an EIS file descriptor, then uses libei to negotiate devices
    and inject input events.
    """

    def __init__(self, dbus_address: str) -> None:
        DBusGMainLoop(set_as_default=True)
        self._bus = dbus.bus.BusConnection(dbus_address)
        self._ei: int = 0  # ctypes void pointer (int representation)
        self._cookie: int = 0
        self._pointer: int = 0  # absolute pointer device
        self._keyboard: int = 0  # keyboard device
        self._touch_device: int = 0  # touch-capable device
        self._text_device: int = 0  # text device (libei >= 1.6)
        self._eis_iface: dbus.Interface | None = None
        self._next_touch_id: int = 0  # auto-increment touch ID
        self._active_touches: dict[int, int] = {}  # touch_id -> ctypes pointer
        # API-level held state (issue #233): evdev codes pressed through the
        # stateful API (keyboard_key_down / mouse_button_down) without the
        # paired release. PAUSED and reconnects reset the server-side logical
        # device state; these sets survive that and are re-pressed on
        # recovery (see _replay_held_state).
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()
        # Start-emulating sequence counter, shared across devices. libei.h
        # (ei_device_start_emulating): "The sequence number identifies this
        # transaction between start/stop emulating. It must go up by at least
        # 1 on each call" — no per-device scoping is required, and a globally
        # monotonic counter satisfies the per-device reading too (B13).
        self._sequence: int = 0
        self._emulating_devices: set[int] = set()  # devices currently in emulating state
        self._connection_dead: bool = False  # set on DISCONNECT (dispatch is void)
        self._setup()

    def _setup(self) -> None:
        """Connect to KWin EIS and negotiate devices.

        Exception-safe on every path after ``connectToEIS`` (issue #229):
        any failure — a NULL context from ``ei_new_sender``, a rejected
        ``ei_setup_backend_fd``, a failed handshake — fully releases the
        occupied state (D-Bus cookie disconnected, EI context unref'd when
        one exists) before the error propagates. In ``__init__`` nobody is
        left to clean up, and every failed reconnect would otherwise leak
        one more live cookie. The guard composes with ``_negotiate_devices``'s
        own teardown (B10): the teardown tolerates already-zeroed slots, so
        the double pass releases nothing twice.

        fd ownership (libei.h: ``ei_setup_backend_fd`` "takes ownership of
        the file descriptor, and will close it when tearing down"): once the
        fd has been handed to libei, the Python side never closes it — a
        double close is worse than the rare leak it would prevent. A fd
        libei never saw (``ei_new_sender`` failed) is closed by the caller.
        """
        # KWin only exposes the EIS interface when it supports remote input;
        # translate the D-Bus failure so callers can treat the input backend as
        # optional (core.py degrades to "no input backend" on RuntimeError;
        # ported from upstream isac322/kwin-mcp#42).
        try:
            eis_obj = self._bus.get_object("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop")
            self._eis_iface = dbus.Interface(eis_obj, "org.kde.KWin.EIS.RemoteDesktop")

            # Request all relevant capabilities
            caps = (
                _EI_CAP_POINTER
                | _EI_CAP_POINTER_ABSOLUTE
                | _EI_CAP_KEYBOARD
                | _EI_CAP_TOUCH
                | _EI_CAP_BUTTON
                | _EI_CAP_SCROLL
            )
            result = self._eis_iface.connectToEIS(dbus.Int32(caps))
        except dbus.DBusException as exc:
            msg = f"KWin EIS interface unavailable: {exc}"
            raise RuntimeError(msg) from exc

        fd = result[0].take()

        fd_seen_by_libei = False
        try:
            # Inside the guard: if int() ever raised, the except path below
            # would close the not-yet-seen fd instead of leaking it (#229).
            self._cookie = int(result[1])
            # Create libei sender context
            self._ei = _get_libei().ei_new_sender(None)
            if not self._ei:
                msg = "Failed to create EI context"
                raise RuntimeError(msg)

            _get_libei().ei_configure_name(self._ei, b"kwin-mcp")

            ret = _get_libei().ei_setup_backend_fd(self._ei, fd)
            # libei accepted the fd (even on a non-zero return it has seen
            # it) — from here on it owns it and closes it when the context
            # is torn down; the Python side must not close it again.
            fd_seen_by_libei = True
            if ret != 0:
                msg = f"ei_setup_backend_fd failed: {ret}"
                raise RuntimeError(msg)

            # Process handshake events to get devices. _negotiate_devices tears
            # the connection down itself on failure (partial handshake = pointer
            # emulating but keyboard missing must not leak zombie state, B10);
            # the guard below makes that double teardown harmless.
            self._negotiate_devices()
        except BaseException:
            if not fd_seen_by_libei:
                # libei never saw the fd — the caller still owns it, and the
                # UnixFD object it came from already detached on take().
                # Suppressed: a failed close (EBADF on an fd already gone)
                # must not mask the primary error or skip the teardown.
                with contextlib.suppress(OSError):
                    os.close(fd)
            self._teardown_connection()
            raise

    def _teardown_connection(self) -> None:
        """Release the whole EIS connection and reset all bookkeeping.

        Safe to call on a partially initialized state: every step tolerates
        zeroed slots and suppresses the recoverable failures (dead D-Bus on
        disconnect, dead socket on the touch cleanup touch_up), so a cleanup
        failure can never abort the teardown halfway and leak devices, the
        EI context or a live D-Bus cookie. Used by the handshake failure
        path, by ``_reconnect`` (F3: the rebuild must be as defensive as
        the handshake teardown, not a less-guarded duplicate of it) and by
        ``close`` (issue #16, round 2: one shared implementation, so the
        ownership model cannot drift between the teardown and the close).

        Ownership is per slot (issue #16, round 2): ``_register_device``
        takes one ``ei_device_ref`` for every slot a handle occupies, so
        every cleared slot releases its own ``ei_device_unref`` here — no
        unique-handle dedup, which would under-release multi-slot handles.
        Emulation state is per handle instead (``_emulating_devices`` is a
        set): a shared handle stops emulating once, then each of its slots
        unrefs. Every per-device step and the context release are
        individually suppressed (issue #16, F4): a failing
        ``stop_emulating``/``unref`` must not skip the remaining slots or
        the context reset below.
        """
        with contextlib.suppress(dbus.DBusException):
            if self._eis_iface and self._cookie:
                self._eis_iface.disconnect(dbus.Int32(self._cookie))
        # The cookie is dropped even if disconnect() raised (F3): a stale
        # cookie must not leak into the fresh handshake after a rebuild.
        self._cookie = 0
        self._invalidate_touches()
        for attr in _DEVICE_ATTRS:
            device = getattr(self, attr)
            setattr(self, attr, 0)
            if not device:
                continue
            # Started devices must stop emulating before release (once per
            # handle: the set discard makes the second slot of a shared
            # handle skip the stop).
            if device in self._emulating_devices:
                with contextlib.suppress(Exception):
                    _get_libei().ei_device_stop_emulating(device)
            self._emulating_devices.discard(device)
            # Per-slot ownership: every cleared slot releases its own ref.
            with contextlib.suppress(Exception):
                _get_libei().ei_device_unref(device)
        self._emulating_devices.clear()
        self._sequence = 0
        if self._ei:
            with contextlib.suppress(Exception):
                _get_libei().ei_unref(self._ei)
            self._ei = 0
        self._connection_dead = False

    def _invalidate_touches(self, *, send_up: bool = True) -> None:
        """Finish and release every active touch gesture, reset the IDs.

        Shared by every path that drops touch state (F4): the reconnect
        teardown, device PAUSED/REMOVED and seat removal. libei's API
        contract is that pausing or removing a device releases all logically
        down touches, and the reconnect unrefs the whole EI context — in all
        these cases the stored touch pointers must not survive.

        ``send_up`` selects finish+release (the device handle is still
        usable — PAUSED, teardown, seat removal, reconnect) versus
        release-only (``False`` — DEVICE_REMOVED, issue #18, R3): sending
        a new ``touch_up`` event into a device the server just dropped is
        protocol-wrong, while the ``unref`` + drop stays required so no
        dangling pointer survives.

        Per-touch failures are suppressed: a dead connection must not abort
        the cleanup before the touches are unref'd and dropped (leaking them
        would leave dangling pointers into a released EI context, A3).

        On a dead or released connection no ``touch_up`` is sent at all
        (issue #16, F6): finishing a gesture into a dead context is
        meaningless — releasing (unref + drop) is the required part. The
        liveness read mirrors the guards the readiness wait uses
        (``_connection_dead`` / ``_ei == 0``).
        """
        live = send_up and not self._connection_dead and self._ei != 0
        for touch in self._active_touches.values():
            if live:
                with contextlib.suppress(Exception):
                    _get_libei().ei_touch_up(touch)
            with contextlib.suppress(Exception):
                _get_libei().ei_touch_unref(touch)
        self._active_touches.clear()
        self._next_touch_id = 0

    def _negotiate_devices(self, timeout: float = 5.0) -> None:
        """Process EIS handshake events until pointer + keyboard are emulating.

        A libei device may only send events once the server has resumed it.
        Calling ``ei_device_start_emulating()`` earlier is rejected ("device
        is not emulating") and every event sent afterwards is silently
        dropped, so wait for ``EI_EVENT_DEVICE_RESUMED`` on each of pointer
        and keyboard before proceeding (adapted from upstream
        isac322/kwin-mcp#42). Unlike upstream, which falls back to an
        unconditional start for devices that never resumed, this fork raises:
        silent input loss is exactly what the wait exists to prevent.

        Emulation starts inside the RESUMED handler (``_resume_device``) so
        the same bookkeeping applies to devices that resume later (touch,
        text) and to re-negotiations after a reconnect.

        On any failure the whole half-initialized state is torn down
        (started devices stop emulating, devices unref'd, EI context
        released, bookkeeping reset) before the error propagates, so a
        failed handshake cannot leak a zombie connection (B10).
        """
        try:
            self._negotiate_devices_inner(timeout)
        except BaseException:
            self._teardown_connection()
            raise

    def _negotiate_devices_inner(self, timeout: float) -> None:
        """Handshake loop proper (teardown handled by ``_negotiate_devices``)."""
        ei_fd = _get_libei().ei_get_fd(self._ei)
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            if self._connection_dead:
                break

            readable, _, _ = select.select([ei_fd], [], [], 0.3)
            if readable:
                # ei_dispatch is void: no return code to inspect. A dead
                # socket surfaces only as a DISCONNECT event, guaranteed to
                # be synthesized by libei on this dispatch and drained below.
                _get_libei().ei_dispatch(self._ei)

            self._drain_events()

            if self._device_emulating(self._pointer) and self._device_emulating(self._keyboard):
                break

        if self._connection_dead:
            # A DISCONNECT event arrived mid-handshake: the registered slots
            # are stale handles of a dead connection, so the handshake must
            # fail instead of reporting success on them (issue #16, F5).
            # The wrapper tears the half-state down.
            msg = "EIS connection lost during handshake (DISCONNECT event)"
            raise RuntimeError(msg)
        if not self._pointer:
            msg = "No pointer device available from EIS"
            raise RuntimeError(msg)
        if not self._keyboard:
            msg = "No keyboard device available from EIS"
            raise RuntimeError(msg)

        missing = [
            name
            for device, name in ((self._pointer, "pointer"), (self._keyboard, "keyboard"))
            if not self._device_emulating(device)
        ]
        if missing:
            tool_error(
                f"EIS input devices did not resume within {timeout}s: {', '.join(missing)}. "
                "Input injection would be silently dropped (libei rejects events sent to "
                "non-resumed devices). Verify the KWin EIS RemoteDesktop interface."
            )

    def _handle_event(self, event: int) -> None:
        """Handle one inbound EIS event affecting our input devices.

        Central dispatcher shared by the handshake, ``_flush`` and the
        device-readiness wait, so PAUSED/REMOVED/RESUMED transitions sent
        mid-session (KWin pauses EIS devices around input bursts) update our
        device bookkeeping wherever they arrive.

        DISCONNECT must not raise from here: a mid-drain exception would skip
        the bookkeeping reset and bypass the reconnect path. It only marks
        the connection dead (``_connection_dead``); the next
        ``_ensure_devices_ready`` call rebuilds the connection.
        """
        etype = _get_libei().ei_event_get_type(event)
        _ei_debug(f"event type={etype}")
        if etype == _EI_EVENT_DISCONNECT:
            self._connection_dead = True
        elif etype == _EI_EVENT_SEAT_REMOVED:
            self._handle_seat_removed()
        elif etype == _EI_EVENT_SEAT_ADDED:
            self._bind_seat_capabilities(event)
        elif etype == _EI_EVENT_DEVICE_ADDED:
            self._register_device(event)
        elif etype == _EI_EVENT_DEVICE_REMOVED:
            self._remove_device(event)
        elif etype == _EI_EVENT_DEVICE_RESUMED:
            self._resume_device(event)
        elif etype == _EI_EVENT_DEVICE_PAUSED:
            self._pause_device(event)
        elif etype == _EI_EVENT_KEYBOARD_MODIFIERS:
            # Server-side modifier-state change (libei 1.6, type 9): purely
            # informational for a sender client. NOT a pause — previously the
            # PAUSED constant was mispinned to 9, so these events parked
            # healthy devices (stop_emulating + reconnects) while real
            # pauses (7) fell through unhandled.
            pass
        else:
            # Unknown event types (e.g. PONG/FRAME on newer libei): release
            # happens in the drain; nothing to bookkeep. Logged for
            # diagnosability under KWIN_MCP_DEBUG_EI=1.
            _ei_debug(f"unhandled EIS event type {etype} (ignored)")

    def _handle_seat_removed(self) -> None:
        """The seat went away: every device on it is gone (unref + forget).

        All logically-down touches die with the seat and are released via
        ``_invalidate_touches`` (F4): their stored pointers must not survive
        the state reset.

        Ownership is per slot (issue #16, round 2): every cleared slot
        releases its own ``ei_device_unref``, matching the per-slot refs
        ``_register_device`` took. Each unref is individually suppressed: a
        failing release must not abort the remaining slots (same guarantee
        as the ``close`` path, M3).
        """
        _ei_debug("seat removed; dropping all devices")
        self._invalidate_touches()
        for attr in _DEVICE_ATTRS:
            device = getattr(self, attr)
            if device:
                self._emulating_devices.discard(device)
                setattr(self, attr, 0)
                with contextlib.suppress(Exception):
                    _get_libei().ei_device_unref(device)
        self._emulating_devices.clear()

    def _resume_device(self, event: int) -> None:
        """Device resumed by the server; request emulation so events flow.

        Idempotent: a duplicate RESUMED for an already-emulating device must
        not restart the emulation sequence (symmetric with ``_pause_device``)
        — nor replay the held key/button state twice (issue #233).
        """
        device = _get_libei().ei_event_get_device(event)
        if device in self._emulating_devices:
            return
        if device in (self._pointer, self._keyboard, self._touch_device, self._text_device):
            self._sequence += 1
            _get_libei().ei_device_start_emulating(device, self._sequence)
            self._emulating_devices.add(device)
            self._replay_held_state(device)

    def _replay_held_state(self, device: int) -> None:
        """Re-press held keys/buttons on a freshly resumed device (issue #233).

        Per the libei API, PAUSED resets the device's logical state to neutral
        ("any keys logically down are released"), and a reconnect starts from
        an equally neutral fresh connection. Without a replay the client's
        held state — presses sent via ``keyboard_key_down`` /
        ``mouse_button_down`` without the paired release — silently diverges
        from the server: held modifiers stop applying and drags lose their
        button mid-gesture.

        Called from ``_resume_device`` right after ``start_emulating`` (libei
        rejects events for non-emulating devices), so the replay frame
        reaches the server before the next user injection. Keys replay on the
        keyboard device in one frame, buttons on the pointer device in one
        frame. Touch gestures are NOT replayed: a paused touch is released
        irreversibly server-side, unlike the discrete key/button state.

        The held sets are created in ``__init__``; objects built without it
        (bare test doubles) simply have nothing to replay.
        """
        held_keys = getattr(self, "_held_keys", None)
        if device == self._keyboard and held_keys:
            for keycode in sorted(held_keys):
                _get_libei().ei_device_keyboard_key(device, keycode, _PRESSED)
            _get_libei().ei_device_frame(device, self._now_us())
        held_buttons = getattr(self, "_held_buttons", None)
        if device == self._pointer and held_buttons:
            for button in sorted(held_buttons):
                _get_libei().ei_device_button_button(device, button, _PRESSED)
            _get_libei().ei_device_frame(device, self._now_us())

    def _pause_device(self, event: int) -> None:
        """Device paused by the server; stop sending events to it.

        Per the libei API, pausing a device resets its logical state to
        neutral — any touches logically down on it are released. When the
        paused device is the touch device, the active gestures are therefore
        finished and dropped here (F4): keeping them in ``_active_touches``
        would let a later touch_move/touch_up drive touches the server has
        already discarded.
        """
        device = _get_libei().ei_event_get_device(event)
        if device in self._emulating_devices:
            _get_libei().ei_device_stop_emulating(device)
            self._emulating_devices.discard(device)
        if device != 0 and device == self._touch_device:
            self._invalidate_touches()

    def _device_emulating(self, device: int) -> bool:
        """Whether the given device is registered and in emulating state."""
        return device != 0 and device in self._emulating_devices

    def _drain_events(self) -> None:
        """Pop every queued inbound event through the central dispatcher.

        Shared by the handshake, the readiness wait and the post-send flush
        so PAUSED/REMOVED/RESUMED transitions are processed wherever they
        arrive instead of sitting queued until the next deadline.

        Every popped event is unref'd exactly once, including on the error
        path (issue #16, F1): ``ei_event_unref`` has a refcount contract and
        a raising handler must not leak the FFI reference.

        The drain stops after a DISCONNECT (issue #16, F5): it is the last
        event of the connection (libei), so anything still queued — e.g. a
        RESUMED — belongs to the dead connection and must not
        ``start_emulating`` or replay held state on it. The teardown of the
        next reconnect releases the whole context, queued events included.
        """
        while True:
            event = _get_libei().ei_get_event(self._ei)
            if not event:
                break
            try:
                self._handle_event(event)
            finally:
                _get_libei().ei_event_unref(event)
            if self._connection_dead:
                break

    def _required_ready(self, require_attrs: tuple[str, ...] = ()) -> bool:
        """Whether pointer + keyboard (plus any extra named slots) emulate.

        Extra slots holding 0 (device not negotiated, e.g. no touch device
        and pointer fallback in use) are skipped: there is nothing to wait
        for. Slot handles are re-read on every call so a reconnect that
        replaced the devices is observed immediately.
        """
        if not (self._device_emulating(self._pointer) and self._device_emulating(self._keyboard)):
            return False
        return all(
            self._device_emulating(getattr(self, attr))
            for attr in require_attrs
            if getattr(self, attr)
        )

    @property
    def has_text_device(self) -> bool:
        """Whether an EIS text device was negotiated (libei >= 1.6 + KWin 6.7+)."""
        return self._text_device != 0

    def _bind_seat_capabilities(self, event: int) -> None:
        """Bind to all available capabilities on the seat."""
        seat = _get_libei().ei_event_get_seat(event)

        bind_list: list[int] = []
        for cap in [
            _EI_CAP_POINTER,
            _EI_CAP_POINTER_ABSOLUTE,
            _EI_CAP_KEYBOARD,
            _EI_CAP_TOUCH,
            _EI_CAP_BUTTON,
            _EI_CAP_SCROLL,
            _EI_CAP_TEXT,
        ]:
            if _get_libei().ei_seat_has_capability(seat, cap):
                bind_list.append(cap)

        # Call variadic ei_seat_bind_capabilities(seat, cap1, ..., NULL)
        func = _get_libei().ei_seat_bind_capabilities
        func.restype = None
        args: list[ctypes.c_uint | ctypes.c_void_p] = [ctypes.c_uint(c) for c in bind_list]
        args.append(ctypes.c_void_p(None))  # NULL sentinel
        func(seat, *args)

    def _register_device(self, event: int) -> None:
        """Register a device from a DEVICE_ADDED event.

        KWin can remove and re-add its EIS devices between input bursts, so
        a re-advertised device must replace any stale reference (adopted from
        01SW/kwin-mcp). But the server may also advertise a second device of
        the same capability while the current one is alive and healthy —
        stealing the slot then would unref a working device mid-flight. A
        slot is only switched when it is empty or the old device is paused
        or already removed from the emulation set (B4).

        Ownership is per slot (issue #16, round 2): every slot the handle
        occupies takes its own ``ei_device_ref`` (see
        ``_replace_device_ref``), and every release path
        (``_remove_device``, ``_handle_seat_removed``,
        ``_teardown_connection``, ``close``) unrefs once per cleared slot,
        so taken and released refs always balance per handle.
        """
        device = _get_libei().ei_event_get_device(event)

        has_abs = _get_libei().ei_device_has_capability(device, _EI_CAP_POINTER_ABSOLUTE)
        has_kbd = _get_libei().ei_device_has_capability(device, _EI_CAP_KEYBOARD)
        has_touch = _get_libei().ei_device_has_capability(device, _EI_CAP_TOUCH)
        has_text = _get_libei().ei_device_has_capability(device, _EI_CAP_TEXT)

        # Prefer absolute pointer device
        if has_abs:
            self._replace_device_ref("_pointer", device)
        if has_kbd:
            self._replace_device_ref("_keyboard", device)
        if has_touch:
            self._replace_device_ref("_touch_device", device)
        if has_text:
            self._replace_device_ref("_text_device", device)

    def _replace_device_ref(self, attr: str, device: int) -> None:
        """Switch a device-slot attribute to a newly advertised device.

        Replacement happens only when the slot is empty, or the current
        occupant is no longer usable (not in the emulating set, i.e. paused
        or removed). A live, emulating device keeps its slot.

        The adopting slot takes its own ``ei_device_ref`` (per-slot
        ownership, issue #16 round 2): evicting this slot later unrefs
        exactly this ref, so a surviving alias in another slot stays valid.
        """
        old = getattr(self, attr)
        if old == device:
            return
        if old and old in self._emulating_devices:
            # Old device still alive and emulating — do not steal the slot.
            return
        if old:
            _get_libei().ei_device_unref(old)
            self._emulating_devices.discard(old)
        setattr(self, attr, _get_libei().ei_device_ref(device))

    def _remove_device(self, event: int) -> None:
        """Drop a device that the server removed (unref + forget).

        Removing a device releases its logically-down touches (libei resets
        the device state), so dropping the touch device also invalidates the
        active gestures (F4) — release-only, without sending a new
        ``touch_up`` into the removed device (issue #18, R3): unlike PAUSED
        (finish + release) the removed handle must receive no new events.

        Ownership is per slot (issue #16, round 2): every cleared slot
        releases its own ``ei_device_unref``, matching the per-slot refs
        ``_register_device`` took for a multi-capability handle. Each unref
        is individually suppressed (issue #18, R1): like the sibling paths
        (``_handle_seat_removed``, ``_teardown_connection``, ``close``) a
        failing release must still clear every slot and run the touch
        cleanup instead of aborting at the first failing slot.
        """
        device = _get_libei().ei_event_get_device(event)
        if not device:
            return
        _ei_debug(f"device removed: {device}")
        # Whether the removed device occupies the touch slot, checked BEFORE
        # the loop clears the slots (comparing after would always see 0).
        was_touch_device = device == self._touch_device
        for attr in _DEVICE_ATTRS:
            if getattr(self, attr) == device:
                setattr(self, attr, 0)
                self._emulating_devices.discard(device)
                with contextlib.suppress(Exception):
                    _get_libei().ei_device_unref(device)
        if was_touch_device:
            self._invalidate_touches(send_up=False)

    def _now_us(self) -> int:
        """Current time in microseconds."""
        return int(time.monotonic() * 1_000_000)

    def _ensure_devices_ready(
        self, timeout_s: float = _STALL_READY_TIMEOUT_S, require_attrs: tuple[str, ...] = ()
    ) -> None:
        """Wait until pointer + keyboard are emulating, reconnecting on failure.

        Callers driving the text or touch device pass it via ``require_attrs``
        (e.g. ``("_text_device",)``): a paused text/touch device would
        otherwise accept the readiness short-circuit on pointer + keyboard
        alone and the injection would be silently dropped. Slot handles are
        resolved fresh on every poll, so a reconnect inside the wait is
        observed immediately.

        This check is best-effort: KWin may pause or remove its devices
        between the readiness check and the actual event injection, and such
        an injection is silently dropped by libei. Batching key strokes into
        a single frame (``keyboard_burst``) minimises that window; the next
        injection's dispatch/drain path recovers (adopted from 01SW/kwin-mcp).

        KWin can remove/re-add or pause its EIS devices around input bursts
        (observed right after session start and after modifier presses).
        Re-advertised devices are re-registered while draining the event
        queue. If the wait fails — devices stalled (paused without resume),
        or the connection was marked dead by a DISCONNECT event or a failed
        ``ei_dispatch`` — the whole connection is rebuilt. A successful
        reconnect guarantees emulating devices, because ``_setup`` runs the
        device negotiation itself and raises on failure.

        Wingman #235: KWin may pause its devices right after EVERY fresh
        handshake (the pause cycle — the fresh connection lives under a
        second, then the cycle repeats), so a single reconnect + re-check
        (issue #228 shape) loses against the cycle. The reconnect is
        therefore retried a bounded number of times (``_RECONNECT_ATTEMPTS``),
        each attempt getting one ``_POST_RECONNECT_READY_TIMEOUT_S`` re-check
        window; exhausting the budget tears the connection down and fails
        loudly with the attempt count. The loop additionally stops starting
        new attempts on the ``_RECONNECT_RETRY_BUDGET_S`` reconnect-attempt
        wall budget (issue #16, F7; issue #18, R2 — checked between
        attempts, not a call-wide deadline: an in-flight handshake may run
        past it): the attempt count alone does not bound one call, and the
        ToolError names the attempts actually made. A
        reconnect that raises inside ``_setup`` (D-Bus/libei/handshake
        error) still aborts immediately: the state is already clean (#229)
        and retrying a hard failure only adds latency. The pre-reconnect
        stall wait was shortened to ``_STALL_READY_TIMEOUT_S`` accordingly.

        After each successful reconnect the requested ``require_attrs`` slots
        are re-checked against the FRESH connection (bounded re-wait, issue
        #228): the fresh handshake only guarantees pointer + keyboard, so a
        text / touch slot may come back paused or unadvertised — such an
        attempt counts as failed and the loop continues. A clean ToolError
        stops the injection after the budget — a silent drop into a paused
        or NULL device is exactly what this gate exists to prevent.
        (A failed reconnect raises inside ``_reconnect`` and never reaches
        the re-check.)

        Wingman #235 attempt 2: when the budget is exhausted, the held
        key/button sets (issue #233) are cleared if non-empty and the
        ToolError names the reset. Per the libei API every PAUSED reset the
        logical state to neutral, so after a lost battle against the pause
        cycle the server holds nothing logically down — the client sets are
        guaranteed stale, and replaying them on the next fresh handshake
        would restart the cycle (a replayed modifier press pauses this KWin
        build again). With the sets cleared, the next call's fresh handshake
        carries no replayed press and the devices stay emulating. The reset
        clause is only added to the message when held state actually
        existed; the plain exhaustion message is unchanged.
        """
        if self._wait_emulating(timeout_s, require_attrs):
            return
        budget_start = time.monotonic()
        attempts_made = 0
        for attempt in range(1, _RECONNECT_ATTEMPTS + 1):
            if time.monotonic() - budget_start >= _RECONNECT_RETRY_BUDGET_S:
                break
            _ei_debug(
                f"devices stalled; reconnecting EIS (attempt {attempt}/{_RECONNECT_ATTEMPTS})"
            )
            try:
                self._reconnect()
            except (ToolError, RuntimeError) as exc:
                tool_error(f"EIS reconnect failed: {exc}")
            attempts_made += 1
            if self._wait_emulating(_POST_RECONNECT_READY_TIMEOUT_S, require_attrs):
                return
        self._teardown_connection()
        attempt_word = "attempt" if attempts_made == 1 else "attempts"
        missing = ", ".join(
            attr.removeprefix("_")
            for attr in require_attrs
            if not self._device_emulating(getattr(self, attr))
        )
        # Wingman #235 attempt 2: reconcile the held state with the server.
        # Per the libei API, PAUSED resets the logical state to neutral ("any
        # buttons or keys logically down are released") and every failed
        # attempt ended in a pause — the server is neutral now, so the held
        # sets are guaranteed stale. Replaying them on the NEXT call's fresh
        # handshake would re-enter the pause cycle forever (observed live:
        # every replayed modifier press pauses this KWin build again, so the
        # budget exhausted on every call once a held state existed). Clearing
        # is a reconciliation, not a silent loss: the ToolError below names
        # the reset, and the next call's fresh handshake carries no replayed
        # press — the devices stay emulating and delivery recovers.
        if self._held_keys or self._held_buttons:
            _ei_debug(
                "held keys/buttons reset after reconnect budget exhaustion "
                f"(keys={sorted(self._held_keys)}, buttons={sorted(self._held_buttons)}); "
                "the server is already in neutral state"
            )
            self._held_keys.clear()
            self._held_buttons.clear()
            tool_error(
                f"EIS did not restore the requested input devices "
                f"({missing or 'pointer/keyboard'}) after {attempts_made} reconnect "
                f"{attempt_word}; injection aborted instead of being silently "
                "dropped into a paused device. Held key/button state was reset "
                "(the server released all logically down input on pause) — "
                "re-press the modifier/button if it is still needed."
            )
        tool_error(
            f"EIS did not restore the requested input devices "
            f"({missing or 'pointer/keyboard'}) after {attempts_made} reconnect "
            f"{attempt_word}; injection aborted instead of being silently "
            "dropped into a paused device."
        )

    def _wait_emulating(self, timeout_s: float, require_attrs: tuple[str, ...] = ()) -> bool:
        """Wait until pointer + keyboard (+ extra slots) emulate, draining events.

        The queue is drained on every iteration (not only when the fd is
        readable): events queued by a previous dispatch would otherwise sit
        unprocessed until the deadline expires. The drain runs BEFORE the
        readiness probe AND before the first ``select`` wait: a queue left by
        a prior loop (e.g. the handshake's last pass on a fresh connection)
        may still hold the DEVICE_ADDED / RESUMED events of a late-resuming
        extra device (issue #228) — probing first would short-circuit on
        pointer + keyboard alone and report ready before those events are
        ever processed, and selecting first would burn up to 50ms before
        already-queued events are even seen (issue #16, F8). A dead
        connection (DISCONNECT event) makes the wait fail immediately so the
        caller takes the reconnect path. ``ei_dispatch`` is void — there is
        no return code to inspect; a dead socket surfaces only as the
        DISCONNECT that libei synthesizes on the next dispatch + drain.

        A NULL (0) EI context fails the wait immediately too (F1): a failed
        reconnect leaves ``_ei == 0``, and probing libei with it
        (``ei_get_fd(0)``) is a hard crash — libei 1.6.0 segfaults on
        ei_get_fd(NULL)/ei_dispatch(NULL), which would kill the whole MCP
        server instead of raising one clean ToolError.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self._connection_dead or self._ei == 0:
                return False
            self._drain_events()
            if self._connection_dead or self._ei == 0:
                return False
            if self._required_ready(require_attrs):
                return True
            if time.monotonic() >= deadline:
                break
            ei_fd = _get_libei().ei_get_fd(self._ei)
            readable, _, _ = select.select([ei_fd], [], [], 0.05)
            if readable:
                # Void dispatch (libei 1.6): nothing to inspect; a synthesized
                # DISCONNECT is observed by the next loop's drain.
                _get_libei().ei_dispatch(self._ei)
        return self._required_ready(require_attrs) and not self._connection_dead

    def _reconnect(self) -> None:
        """Rebuild the EIS connection after the server stopped negotiating.

        KWin pauses EIS devices (e.g. after the client presses a modifier)
        without resuming them; a fresh connectToEIS restores the session
        (adopted from 01SW/kwin-mcp).

        F3: the rebuild is ``_teardown_connection()`` + ``_setup()`` — the
        same fully-defensive cleanup the handshake failure path uses, not a
        hand-rolled duplicate. Previously the inline copy skipped
        ``ei_device_stop_emulating`` before unref, suppressed neither the
        touch-cleanup ``ei_touch_up`` nor the D-Bus ``disconnect`` failure,
        and left a stale cookie; any of those could abort the rebuild
        halfway, leaving the client dirty so the next injection repeated the
        same failure.

        The old EI context is unref'd here, so all libei objects belonging
        to it (device handles, active touch sequences) are stale afterwards:
        touches are finished and dropped, devices and the context are
        unref'd, bookkeeping is reset.
        """
        self._teardown_connection()
        self._setup()

    def _flush(self) -> None:
        """Dispatch pending events to send data to KWin, then drain replies.

        Draining processes device pause/resume/remove events so our
        emulation bookkeeping stays in sync with the server between
        injections. ``ei_dispatch`` is void (libei 1.6): there is no return
        code to branch on — a dead socket surfaces as the DISCONNECT event
        that libei synthesizes into the queue, and the drain below is what
        observes it. A DISCONNECT drained here means this operation's
        events were already handed to libei but delivery is unconfirmed:
        raise ToolError instead of reporting a silent success (honest
        delivery, issue #234 on the real API). The next injection's
        readiness check sees the dead flag and rebuilds the connection.

        Pre-send drains (the readiness gate) stay non-raising: nothing has
        been sent yet, so a DISCONNECT observed there only fails the wait
        and the caller takes the reconnect path.

        Only the post-send injection paths call this method; the cleanup
        paths (``_teardown_connection`` → ``_invalidate_touches``, ``close``)
        talk to libei directly and stay exception-tolerant.

        A NULL (0) EI context marks the connection dead and raises ToolError
        (issue #16, F3): ``ei_dispatch(0)`` segfaults, no dispatch is ever
        meaningful on a torn-down connection — and a silent return would
        report success for an injection that never reached libei, against
        the honest-delivery contract above. (Unreachable after a
        successful readiness gate — a failed gate raises before any
        injection runs.)

        Raises:
            ToolError: when the post-send drain observed the connection's
                DISCONNECT — the events were sent but delivery is
                unconfirmed — or when the EI context is already released.
        """
        if self._ei == 0:
            self._connection_dead = True
            tool_error(
                "EIS context is released (no live connection); the injection "
                "was not delivered — the next call rebuilds the connection."
            )
        _get_libei().ei_dispatch(self._ei)
        self._drain_events()
        if self._connection_dead:
            tool_error(
                "input delivery failed; EIS connection lost (disconnect) — "
                "delivery unconfirmed; the next call rebuilds the connection "
                "and delivers."
            )

    def pointer_move_absolute(self, x: float, y: float) -> None:
        """Move pointer to absolute coordinates."""
        self._ensure_devices_ready()
        _get_libei().ei_device_pointer_motion_absolute(self._pointer, x, y)
        _get_libei().ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_button(self, button: int, state: int) -> None:
        """Press/release a mouse button (evdev button code)."""
        self._ensure_devices_ready()
        _get_libei().ei_device_button_button(self._pointer, button, state)
        _get_libei().ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def hold_button(self, button: int) -> None:
        """Press a mouse button and track it as logically down (issue #233).

        The stateful half of ``mouse_button_down``: the press is recorded so a
        PAUSED/reconnect recovery replays it (``_replay_held_state``);
        ``release_button`` is the pairing half. Non-stateful presses (clicks,
        drags) must not go through here — they would turn transient presses
        into eternal holds.

        The intent is recorded BEFORE the send (issue #19): the post-send
        ``_flush`` drains a PAUSED→RESUMED arriving in the same call, and the
        replay must already see the new button — recording after the flush
        misses that same-call recovery and diverges from the server. On a
        send failure (``ToolError`` from the readiness gate or the flush)
        the intent rolls back, so the client never claims a button the
        server did not confirm held.
        """
        was_held = button in self._held_buttons
        self._held_buttons.add(button)
        try:
            self.pointer_button(button, _PRESSED)
        except ToolError:
            if not was_held:
                self._held_buttons.discard(button)
            raise

    def release_button(self, button: int) -> None:
        """Release a mouse button and drop it from the held set (issue #233).

        The intent is dropped BEFORE the send (issue #19): the post-send
        ``_flush`` drains a PAUSED→RESUMED arriving in the same call, and the
        replay must NOT see the released button — dropping after the flush
        re-presses it on the wire (sticky button). On a send failure
        (``ToolError``) the button is restored to the held set when it was
        held, so the client never claims a release the server did not
        confirm.
        """
        was_held = button in self._held_buttons
        self._held_buttons.discard(button)
        try:
            self.pointer_button(button, _RELEASED)
        except ToolError:
            if was_held:
                self._held_buttons.add(button)
            raise

    def pointer_scroll(self, dx: float, dy: float) -> None:
        """Scroll by pixel delta."""
        self._ensure_devices_ready()
        _get_libei().ei_device_scroll_delta(self._pointer, dx, dy)
        _get_libei().ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_scroll_discrete(self, dx: int, dy: int) -> None:
        """Scroll by discrete steps (wheel ticks)."""
        self._ensure_devices_ready()
        _get_libei().ei_device_scroll_discrete(self._pointer, dx, dy)
        _get_libei().ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def pointer_scroll_stop(self) -> None:
        """Signal end of scroll."""
        self._ensure_devices_ready()
        _get_libei().ei_device_scroll_stop(self._pointer, 1, 1)
        _get_libei().ei_device_frame(self._pointer, self._now_us())
        self._flush()

    def keyboard_key(self, keycode: int, state: int) -> None:
        """Press/release a key (evdev keycode)."""
        self._ensure_devices_ready()
        _get_libei().ei_device_keyboard_key(self._keyboard, keycode, state)
        _get_libei().ei_device_frame(self._keyboard, self._now_us())
        self._flush()

    def keyboard_burst(self, pairs: list[tuple[int, int]]) -> None:
        """Send several key events in a single frame.

        KWin pauses its EIS devices mid-frame when a modifier combination
        (e.g. alt+F4) spans multiple frames; batching the press strokes in
        one frame keeps the combination intact (adopted from 01SW/kwin-mcp).
        """
        self._ensure_devices_ready()
        for keycode, state in pairs:
            _get_libei().ei_device_keyboard_key(self._keyboard, keycode, state)
        _get_libei().ei_device_frame(self._keyboard, self._now_us())
        self._flush()

    def hold_keys(self, keycodes: list[int]) -> None:
        """Send key presses and track them as logically down (issue #233).

        The stateful half of ``keyboard_key_down``: the pressed codes are
        recorded so a PAUSED/reconnect recovery replays them
        (``_replay_held_state``); ``release_keys`` is the pairing half.
        Ordinary press+release bursts (key combos, click modifiers) must not
        go through here — they would turn transient presses into eternal
        holds.

        The intent is recorded BEFORE the send (issue #19): the post-send
        ``_flush`` drains a PAUSED→RESUMED arriving in the same call, and the
        replay must already see the new keys — recording after the flush
        misses that same-call recovery and diverges from the server. On a
        send failure (``ToolError`` from the readiness gate or the flush)
        the newly added intents roll back, so the client never claims keys
        the server did not confirm held.
        """
        if not keycodes:
            return
        fresh = set(keycodes) - self._held_keys
        self._held_keys.update(keycodes)
        try:
            self.keyboard_burst([(code, _PRESSED) for code in keycodes])
        except ToolError:
            self._held_keys.difference_update(fresh)
            raise

    def release_keys(self, keycodes: list[int]) -> None:
        """Send key releases and drop them from the held set (issue #233).

        Releases in list order (``keyboard_key_up`` passes the main key
        first, then the reversed modifiers).

        The intent is dropped BEFORE the send (issue #19): the post-send
        ``_flush`` drains a PAUSED→RESUMED arriving in the same call, and the
        replay must NOT see the released keys — dropping after the flush
        re-presses them on the wire (sticky modifier). On a send failure
        (``ToolError``) the previously held intents are restored, so the
        client never claims a release the server did not confirm.
        """
        if not keycodes:
            return
        held_before = set(keycodes) & self._held_keys
        self._held_keys.difference_update(keycodes)
        try:
            self.keyboard_burst([(code, _RELEASED) for code in keycodes])
        except ToolError:
            self._held_keys.update(held_before)
            raise

    def claim_transient_hold(
        self, keys: list[int], buttons: list[int]
    ) -> tuple[frozenset[int], frozenset[int]]:
        """Register operation-scoped transient presses as temporary held intents.

        Composite operations (``InputBackend.mouse_click`` modifiers,
        ``InputBackend.mouse_drag`` modifiers + drag button) send presses
        whose release belongs to the same call. Such transient presses used
        to bypass the held sets, so a PAUSED/reconnect recovery
        mid-operation silently dropped them while the operation continued:
        the click landed without its modifier and the drag degraded to a
        button-less motion (issue #20). Registered here, a mid-operation
        recovery replays them through the production ``_replay_held_state``
        path and the operation completes with modifiers/button intact.

        The intents are recorded BEFORE the operation's press frames go out
        (issue #19 ordering): a PAUSED→RESUMED drained by the press frame's
        own post-send ``_flush`` must already see them, or the replay misses
        the same-call recovery. ``drop_transient_hold`` releases them; both
        methods send nothing themselves.

        Pre-existing membership (a modifier the agent holds across calls via
        ``keyboard_key_down``) is remembered in the return value and never
        stolen: the drop removes only what this claim added.

        Args:
            keys: Evdev keycodes to hold transiently (modifier DOWN set).
            buttons: Evdev button codes to hold transiently (drag button).

        Returns:
            The (key, button) codes that were already held before this
            claim, to hand back to ``drop_transient_hold``.
        """
        pre_keys = frozenset(keys) & self._held_keys
        pre_buttons = frozenset(buttons) & self._held_buttons
        self._held_keys.update(keys)
        self._held_buttons.update(buttons)
        return (pre_keys, pre_buttons)

    def drop_transient_hold(
        self,
        keys: list[int],
        buttons: list[int],
        pre: tuple[frozenset[int], frozenset[int]],
    ) -> None:
        """Release operation-scoped transient intents, keeping other holds.

        Pairing half of ``claim_transient_hold``: drops only the codes this
        claim added. Pre-existing holds (remembered in ``pre``) were never
        removed by the claim and stay exactly as the recovery path left
        them — in particular a reconnect-budget exhaustion that cleared the
        sets mid-operation (with its "re-press if needed" ToolError) is not
        undone here.

        Called BEFORE the operation's release frames go out (issue #19
        release ordering): a PAUSED→RESUMED drained by the release frame's
        own ``_flush`` must find nothing transient to re-press, or the wire
        ends DOWN-after-UP (sticky modifier). Idempotent: safe to call again
        on the operation's failure path after the success path already
        dropped.

        Sends nothing itself; a release frame whose delivery fails
        (ToolError) still ends the operation scope, so the transient intents
        stay dropped instead of leaking into the next handshake's replay as
        phantom holds.
        """
        pre_keys, pre_buttons = pre
        self._held_keys.difference_update(set(keys) - pre_keys)
        self._held_buttons.difference_update(set(buttons) - pre_buttons)

    def text_keysym(self, keysym: int, state: int) -> None:
        """Press/release a key by XKB keysym via the EIS text device.

        Requires libei >= 1.6 and a KWin EIS server with TEXT support; the
        server resolves the keysym through its own keymap, so the client needs
        no keymap knowledge. The slot is re-checked after the readiness gate
        (issue #228): a reconnect inside it may leave the text slot empty on
        the fresh connection, and the NULL guard must fail the injection with
        a clean ToolError instead of sending the keysym into device 0.
        """
        if not self._text_device:
            msg = "No EIS text device available (libei >= 1.6 required)"
            raise RuntimeError(msg)
        self._ensure_devices_ready(require_attrs=("_text_device",))
        if not self._text_device:
            tool_error(
                "EIS text device was not re-negotiated after the EIS reconnect "
                "(the fresh connection did not advertise one); injection aborted "
                "instead of being sent into a NULL device."
            )
        _get_libei().ei_device_text_keysym(self._text_device, keysym, state)
        _get_libei().ei_device_frame(self._text_device, self._now_us())
        self._flush()

    def text_utf8(self, text: str) -> None:
        """Send a UTF-8 string through the EIS text device.

        The server injects it via its input method (KWin: inputMethod()->sendText).
        The slot is re-checked after the readiness gate (issue #228), mirroring
        ``text_keysym``: a reconnect may leave the fresh connection without a
        text device, and the NULL guard fails cleanly rather than silently
        dropping the text.
        """
        if not self._text_device:
            msg = "No EIS text device available (libei >= 1.6 required)"
            raise RuntimeError(msg)
        self._ensure_devices_ready(require_attrs=("_text_device",))
        if not self._text_device:
            tool_error(
                "EIS text device was not re-negotiated after the EIS reconnect "
                "(the fresh connection did not advertise one); injection aborted "
                "instead of being sent into a NULL device."
            )
        _get_libei().ei_device_text_utf8(self._text_device, text.encode("utf-8"))
        _get_libei().ei_device_frame(self._text_device, self._now_us())
        self._flush()

    def touch_down(self, x: float, y: float) -> int:
        """Start a new touch at (x, y). Returns a touch ID.

        Requires a negotiated EIS touch device: the touch is created on it
        directly (issue #24). The old pointer fallback created a touch
        object on a device without touchscreen capability — libei resolves
        that to a NULL touchscreen, a wire error that ends in
        ``ei_disconnect`` — so a touch-less connection now fails cleanly
        with a ToolError instead of killing the connection.
        """
        self._ensure_devices_ready(require_attrs=("_touch_device",))
        if self._touch_device == 0:
            tool_error("No EIS touch device on this connection")
        device = self._touch_device
        touch = _get_libei().ei_device_touch_new(device)
        if not touch:
            msg = "Failed to create touch object"
            raise RuntimeError(msg)
        _get_libei().ei_touch_down(touch, x, y)
        _get_libei().ei_device_frame(device, self._now_us())
        try:
            self._flush()
        except ToolError:
            # The gesture object is registered (and its ownership handed to
            # the caller via the returned ID) only AFTER a successful flush.
            # On a delivery failure unref the orphaned touch here, or it
            # leaks: it is neither in _active_touches (so no reconnect
            # teardown can release it) nor owned by the caller.
            _get_libei().ei_touch_unref(touch)
            raise

        touch_id = self._next_touch_id
        self._next_touch_id += 1
        self._active_touches[touch_id] = touch
        return touch_id

    def touch_move(self, touch_id: int, x: float, y: float) -> None:
        """Move an active touch to (x, y).

        The readiness check runs BEFORE the touch pointer is fetched (F2,
        formerly W2): ``_ensure_devices_ready`` may rebuild the connection,
        and a rebuild finishes and drops all active gestures — a pointer
        captured before it would be dangling into the unref'd EI context
        (use-after-free). Fetching after it means the touch is either still
        alive (context unchanged, dict untouched) or gone (reconnect or a
        server-side PAUSED/REMOVED invalidated it): a dead gesture raises
        ValueError here, never touches freed memory. A reconnect ends every
        active gesture; a gesture cannot be continued across one — start a
        new touch instead.
        """
        self._ensure_devices_ready(require_attrs=("_touch_device",))
        touch = self._active_touches.get(touch_id)
        if touch is None:
            msg = f"No active touch with ID {touch_id}"
            raise ValueError(msg)
        if self._touch_device == 0:
            # Mirror touch_down: a reconnect during the gesture could leave
            # the fresh connection without a touch device — never send the
            # motion into a non-touchscreen device (wire error → ei_disconnect).
            tool_error("No EIS touch device on this connection")
        _get_libei().ei_touch_motion(touch, x, y)
        _get_libei().ei_device_frame(self._touch_device, self._now_us())
        self._flush()

    def touch_up(self, touch_id: int) -> None:
        """End an active touch.

        Same ensure-first ordering as ``touch_move`` (F2, formerly W2): the
        ID is resolved to a pointer only after the readiness check, and the
        dict entry is popped only afterwards — the reconnect inside
        ``_ensure_devices_ready`` cleans up the gestures itself, so popping
        first would both skip that cleanup and leave the method holding a
        dangling pointer for its own ``ei_touch_up``.
        """
        self._ensure_devices_ready(require_attrs=("_touch_device",))
        touch = self._active_touches.pop(touch_id, None)
        if touch is None:
            msg = f"No active touch with ID {touch_id}"
            raise ValueError(msg)
        if self._touch_device == 0:
            # Mirror touch_down: the gesture is already popped (finished
            # client-side) — no device event is sent into a non-touchscreen
            # device (wire error → ei_disconnect), only the object release.
            _get_libei().ei_touch_unref(touch)
            tool_error("No EIS touch device on this connection")
        _get_libei().ei_touch_up(touch)
        _get_libei().ei_device_frame(self._touch_device, self._now_us())
        _get_libei().ei_touch_unref(touch)
        self._flush()

    def close(self) -> None:
        """Clean up EIS connection.

        Delegates to ``_teardown_connection`` (issue #16, round 2, M3): the
        previous hand-rolled cleanup aborted on the first failing step, kept
        its own per-slot bookkeeping next to the teardown's unique-handle
        dedup (the two models disagreed on multi-capability handles — the
        shared handle was stopped and unref'd twice while an aliased touch
        slot survived stale), and therefore drifted from every hardened
        path. One shared implementation releases touches, drops the D-Bus
        cookie, stops each handle once, unrefs once per slot and releases
        the EI context — every step individually suppressed, so a failing
        cleanup step can never abort the rest.
        """
        self._teardown_connection()


class InputBackend:
    """High-level input injection for an isolated KWin session.

    Wraps EISClient with convenient methods for mouse and keyboard
    operations including click, drag, scroll, and key combos.
    """

    def __init__(self, dbus_address: str) -> None:
        self._client = EISClient(dbus_address)

    def mouse_move(self, x: int, y: int) -> None:
        """Move mouse to absolute coordinates (hover)."""
        self._client.pointer_move_absolute(float(x), float(y))

    def mouse_click(
        self,
        x: int,
        y: int,
        button: MouseButton = MouseButton.LEFT,
        *,
        double: bool = False,
        click_count: int = 1,
        modifiers: list[str] | None = None,
        hold_ms: int = 0,
    ) -> None:
        """Click at the given coordinates.

        Recovery semantics (issue #20): transient modifiers are registered
        as temporary held state for the operation duration, so an EIS
        recovery (PAUSED/reconnect) mid-click replays them through the
        production held-state path and the click lands WITH its modifiers.
        The sets are empty again afterwards; a mid-operation
        reconnect-budget exhaustion still aborts loudly with its own
        "re-press if needed" ToolError instead of a silent modifier-less
        click.

        Args:
            x, y: Coordinates to click at.
            button: Mouse button to use.
            double: If True, double-click (shorthand for click_count=2).
            click_count: Number of consecutive clicks (1=single, 2=double, 3=triple).
            modifiers: Modifier keys to hold during click (e.g. ["ctrl"], ["shift", "alt"]).
            hold_ms: Duration to hold button pressed before release (for long-press).
        """
        if double and click_count == 1:
            click_count = 2

        btn_code = _BTN_CODES[button]
        mod_codes = _resolve_modifiers(modifiers)

        self.mouse_move(x, y)
        time.sleep(0.02)

        # Batch modifier presses into one frame: a combo spread over several
        # frames lets KWin pause the keyboard device mid-press and drop the
        # remaining strokes (same rationale as keyboard_burst for key combos).
        pre = self._client.claim_transient_hold(mod_codes, [])
        try:
            if mod_codes:
                self._client.keyboard_burst([(mod, _PRESSED) for mod in mod_codes])

            for i in range(click_count):
                if i > 0:
                    time.sleep(0.05)
                self._client.pointer_button(btn_code, _PRESSED)
                if hold_ms > 0 and i == click_count - 1:
                    time.sleep(max(0.01, hold_ms / 1000.0))
                else:
                    time.sleep(0.01)
                self._client.pointer_button(btn_code, _RELEASED)
        finally:
            # Drop BEFORE the release frame (issue #19 release ordering): a
            # PAUSED→RESUMED drained by the release's own post-send _flush
            # must find nothing transient to re-press (sticky modifier).
            self._client.drop_transient_hold(mod_codes, [], pre)

        # Release modifier keys in reverse order, again as a single frame.
        if mod_codes:
            self._client.keyboard_burst([(mod, _RELEASED) for mod in reversed(mod_codes)])

    def mouse_scroll(
        self,
        x: int,
        y: int,
        delta: int,
        *,
        horizontal: bool = False,
        discrete: bool = False,
        steps: int = 1,
    ) -> None:
        """Scroll at the given coordinates.

        Args:
            x, y: Coordinates to scroll at.
            delta: Scroll amount (positive = down/right, negative = up/left).
            horizontal: If True, scroll horizontally.
            discrete: If True, use discrete scroll (wheel ticks) instead of smooth pixels.
            steps: Split total delta into this many increments with 10ms intervals.
        """
        self.mouse_move(x, y)
        time.sleep(0.02)

        if discrete:
            dx = delta if horizontal else 0
            dy = delta if not horizontal else 0
            if steps > 1:
                for i in range(steps):
                    frac_dx = dx // steps + (1 if i < dx % steps else 0) if dx else 0
                    frac_dy = dy // steps + (1 if i < dy % steps else 0) if dy else 0
                    if frac_dx or frac_dy:
                        self._client.pointer_scroll_discrete(frac_dx, frac_dy)
                    time.sleep(0.01)
            else:
                self._client.pointer_scroll_discrete(dx, dy)
            self._client.pointer_scroll_stop()
        else:
            total_dx = float(delta) * _SCROLL_STEP_PIXELS if horizontal else 0.0
            total_dy = float(delta) * _SCROLL_STEP_PIXELS if not horizontal else 0.0
            if steps > 1:
                step_dx = total_dx / steps
                step_dy = total_dy / steps
                for _ in range(steps):
                    self._client.pointer_scroll(step_dx, step_dy)
                    time.sleep(0.01)
            else:
                self._client.pointer_scroll(total_dx, total_dy)
            self._client.pointer_scroll_stop()

    def mouse_drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        button: MouseButton = MouseButton.LEFT,
        modifiers: list[str] | None = None,
        waypoints: list[tuple[int, int, int]] | None = None,
    ) -> None:
        """Drag from one point to another.

        Recovery semantics (issue #20): transient modifiers and the drag
        button are registered as temporary held state for the operation
        duration, so an EIS recovery (PAUSED/reconnect) mid-drag replays
        them through the production held-state path — the motion frames run
        with the button logically down, never as a button-less motion. The
        sets are empty again afterwards; a mid-operation reconnect-budget
        exhaustion still aborts loudly with its own "re-press if needed"
        ToolError instead of a silent modifier-less drag.

        Args:
            from_x, from_y: Starting coordinates.
            to_x, to_y: Ending coordinates.
            button: Mouse button to use for dragging.
            modifiers: Modifier keys to hold during drag (e.g. ["alt"], ["ctrl"]).
            waypoints: Intermediate points as (x, y, dwell_ms) tuples.
        """
        btn_code = _BTN_CODES[button]
        mod_codes = _resolve_modifiers(modifiers)

        self.mouse_move(from_x, from_y)
        time.sleep(0.05)

        # Batch modifier presses into one frame (same rationale as in
        # mouse_click: KWin may pause mid-press otherwise).
        pre = self._client.claim_transient_hold(mod_codes, [btn_code])
        try:
            if mod_codes:
                self._client.keyboard_burst([(mod, _PRESSED) for mod in mod_codes])

            self._client.pointer_button(btn_code, _PRESSED)
            time.sleep(0.02)

            # Build full path: start -> waypoints -> end
            segments: list[tuple[int, int, int, int, int]] = []  # (fx, fy, tx, ty, dwell_ms)
            prev_x, prev_y = from_x, from_y
            if waypoints:
                for wx, wy, dwell_ms in waypoints:
                    segments.append((prev_x, prev_y, wx, wy, dwell_ms))
                    prev_x, prev_y = wx, wy
            segments.append((prev_x, prev_y, to_x, to_y, 0))

            for seg_fx, seg_fy, seg_tx, seg_ty, dwell_ms in segments:
                dx = seg_tx - seg_fx
                dy = seg_ty - seg_fy
                steps = max(10, int((dx**2 + dy**2) ** 0.5 / 10))
                for i in range(1, steps + 1):
                    frac = i / steps
                    cx = seg_fx + dx * frac
                    cy = seg_fy + dy * frac
                    self._client.pointer_move_absolute(cx, cy)
                    time.sleep(0.01)
                if dwell_ms > 0:
                    time.sleep(dwell_ms / 1000.0)

            time.sleep(0.02)
            # Drop the transient BUTTON hold BEFORE the release frame (issue
            # #24 release ordering): a PAUSED→RESUMED drained by the
            # release's own post-send _flush must find nothing transient to
            # re-press, or the wire ends DOWN-after-UP (sticky drag button).
            # Pre-existing cross-call holds survive: drop_transient_hold
            # removes only what this claim added. The release frame's
            # delivery failure (ToolError) still ends the operation scope —
            # the transient intents stay dropped (see drop_transient_hold).
            self._client.drop_transient_hold([], [btn_code], (frozenset(), pre[1]))
            self._client.pointer_button(btn_code, _RELEASED)
        finally:
            # Remaining drop: the modifier keys (and the button idempotently,
            # for failure paths that aborted before the release frame).
            self._client.drop_transient_hold(mod_codes, [btn_code], pre)

        # Release modifier keys in reverse order, again as a single frame.
        if mod_codes:
            self._client.keyboard_burst([(mod, _RELEASED) for mod in reversed(mod_codes)])

    def mouse_button_down(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """Move to coordinates and press a mouse button without releasing.

        The press is tracked client-side and replayed automatically after an
        EIS recovery (PAUSED/reconnect), so a drag held across several MCP
        calls keeps its button down (issue #233).

        Args:
            x, y: Coordinates.
            button: Mouse button to press.
        """
        btn_code = _BTN_CODES[button]
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._client.hold_button(btn_code)

    def mouse_button_up(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """Move to coordinates and release a mouse button.

        Drops the button from the held set, so a subsequent recovery does not
        replay the release.

        Args:
            x, y: Coordinates.
            button: Mouse button to release.
        """
        btn_code = _BTN_CODES[button]
        self.mouse_move(x, y)
        time.sleep(0.02)
        self._client.release_button(btn_code)

    def keyboard_type(self, text: str) -> None:
        """Type a string of text character by character.

        Uses the EIS text device (keysym per character, resolved server-side)
        when available (libei >= 1.6 + KWin 6.7+). This delivers keys regardless
        of which keymap the server uses, unlike the bare-keycode path which
        assumes a US QWERTY keymap. Falls back to the legacy keycode path on
        servers without TEXT support.
        """
        if self._client.has_text_device:
            for char in text:
                keysym = ascii_char_to_keysym(char)
                if keysym is None:
                    continue  # non-ASCII → use keyboard_type_unicode instead
                self._client.text_keysym(keysym, _PRESSED)
                time.sleep(0.01)
                self._client.text_keysym(keysym, _RELEASED)
                time.sleep(0.02)
            return

        # Legacy path: bare evdev keycodes (assumes US QWERTY keymap server-side)
        for char in text:
            entry = _CHAR_KEY_MAP.get(char)
            if entry is None:
                continue

            keycode, needs_shift = entry
            if needs_shift:
                self._client.keyboard_key(_MODIFIER_KEYS["shift"], _PRESSED)
                time.sleep(0.01)

            self._client.keyboard_key(keycode, _PRESSED)
            time.sleep(0.01)
            self._client.keyboard_key(keycode, _RELEASED)

            if needs_shift:
                time.sleep(0.01)
                self._client.keyboard_key(_MODIFIER_KEYS["shift"], _RELEASED)

            time.sleep(0.02)

    def _press_key_combo(self, key: str) -> None:
        """Press one parsed modifier combo via the bare-keycode path.

        This is ``keyboard_key``'s core without the ctrl+q alias dispatch, so
        the alias can send each combo exactly once without recursing into
        itself.
        """
        modifiers, keycode = _parse_key_combo(key)
        if keycode is None:
            return

        if not modifiers and self._client.has_text_device:
            # Text/special keys resolved server-side by keysym
            keysym = key_name_to_keysym(key) or (
                ascii_char_to_keysym(key) if len(key) == 1 else None
            )
            if keysym is not None:
                self._client.text_keysym(keysym, _PRESSED)
                time.sleep(0.01)
                self._client.text_keysym(keysym, _RELEASED)
                return

        # Batch the press strokes into one frame: KWin pauses EIS devices
        # mid-frame when a modifier combination spans multiple frames
        # (adopted from 01SW/kwin-mcp).
        self._client.keyboard_burst([(m, _PRESSED) for m in modifiers] + [(keycode, _PRESSED)])
        time.sleep(0.02)
        release_pairs = [(keycode, _RELEASED)] + [(m, _RELEASED) for m in reversed(modifiers)]
        self._client.keyboard_burst(release_pairs)

    def keyboard_key(self, key: str) -> None:
        """Press a key combination (e.g., 'ctrl+c', 'Return', 'alt+F4').

        Supports modifier combinations with '+' separator. Bare keys (no
        modifiers) are routed through the EIS text device when available, so
        the server resolves them via its own keymap; modifier combos keep the
        bare-keycode path (which works reliably for shortcuts).
        """
        # KDE splits window-close across two bindings by ACCEL convention:
        # Konsole binds it to Ctrl+Shift+Q (its ACCEL convention is
        # Ctrl+Shift), while most other KDE apps (kwrite, kcalc) bind plain
        # Ctrl+Q — each combo is unbound in the other apps. Send both with a
        # short pause (mirroring the paste alias below); on apps that bind
        # only one of them the other is an inert no-op shortcut, so "quit the
        # focused app" behaves uniformly across KDE apps.
        if key.lower() in ("ctrl+q", "control+q", "ctrl+quit"):
            self._press_key_combo("ctrl+q")
            time.sleep(0.15)
            self._press_key_combo("ctrl+shift+q")
            return

        self._press_key_combo(key)

    def keyboard_key_down(self, key: str) -> None:
        """Press (and hold) a key combination without releasing.

        Useful for holding modifier keys across multiple actions. The pressed
        codes are tracked client-side and replayed automatically after an EIS
        recovery (PAUSED/reconnect), so the modifier stays held across MCP
        calls even when KWin resets the device state in between (issue #233).

        Args:
            key: Key to press (e.g., "ctrl", "shift+a", "alt").
        """
        modifiers, keycode = _parse_key_combo(key)

        # Single frame like _press_key_combo: KWin may pause mid-press.
        codes = [*modifiers, keycode] if keycode is not None else list(modifiers)
        if codes:
            self._client.hold_keys(codes)

    def keyboard_key_up(self, key: str) -> None:
        """Release a previously pressed key combination.

        Releases in reverse order (main key first, then modifiers) and drops
        the codes from the held set, so a subsequent recovery does not replay
        the released keys.

        Args:
            key: Key to release (e.g., "ctrl", "shift+a", "alt").
        """
        modifiers, keycode = _parse_key_combo(key)

        release_codes = [keycode] if keycode is not None else []
        release_codes.extend(reversed(modifiers))
        if release_codes:
            self._client.release_keys(release_codes)

    def touch_tap(self, x: int, y: int, hold_ms: int = 0) -> None:
        """Tap at the given coordinates.

        Args:
            x, y: Coordinates to tap at.
            hold_ms: Duration to hold before lifting (for long-press).
        """
        tid = self._client.touch_down(float(x), float(y))
        if hold_ms > 0:
            time.sleep(max(0.01, hold_ms / 1000.0))
        else:
            time.sleep(0.01)
        self._client.touch_up(tid)

    def touch_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from one point to another.

        Args:
            from_x, from_y: Starting coordinates.
            to_x, to_y: Ending coordinates.
            duration_ms: Duration of the swipe in milliseconds.
        """
        steps = max(10, duration_ms // 10)
        dx = to_x - from_x
        dy = to_y - from_y

        tid = self._client.touch_down(float(from_x), float(from_y))
        step_delay = max(0.001, duration_ms / 1000.0 / steps)

        for i in range(1, steps + 1):
            frac = i / steps
            cx = from_x + dx * frac
            cy = from_y + dy * frac
            self._client.touch_move(tid, cx, cy)
            time.sleep(step_delay)

        self._client.touch_up(tid)

    def touch_pinch(
        self,
        center_x: int,
        center_y: int,
        start_distance: int,
        end_distance: int,
        duration_ms: int = 500,
    ) -> None:
        """Pinch gesture with two fingers.

        Args:
            center_x, center_y: Center point of the pinch.
            start_distance: Initial distance between fingers (pixels).
            end_distance: Final distance between fingers (pixels).
            duration_ms: Duration of the gesture.
        """
        steps = max(10, duration_ms // 10)
        step_delay = max(0.001, duration_ms / 1000.0 / steps)

        # Two fingers start symmetrically on the x-axis
        half_start = start_distance / 2.0
        tid1 = self._client.touch_down(float(center_x - half_start), float(center_y))
        tid2 = self._client.touch_down(float(center_x + half_start), float(center_y))

        for i in range(1, steps + 1):
            frac = i / steps
            half = half_start + (end_distance / 2.0 - half_start) * frac
            self._client.touch_move(tid1, float(center_x - half), float(center_y))
            self._client.touch_move(tid2, float(center_x + half), float(center_y))
            time.sleep(step_delay)

        self._client.touch_up(tid1)
        self._client.touch_up(tid2)

    def touch_multi_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        fingers: int = 3,
        duration_ms: int = 300,
    ) -> None:
        """Multi-finger swipe gesture.

        Args:
            from_x, from_y: Starting coordinates (center of finger group).
            to_x, to_y: Ending coordinates.
            fingers: Number of fingers (2-5).
            duration_ms: Duration of the swipe.
        """
        steps = max(10, duration_ms // 10)
        dx = to_x - from_x
        dy = to_y - from_y
        step_delay = max(0.001, duration_ms / 1000.0 / steps)
        finger_spacing = 20  # pixels between fingers

        # Start touches spread vertically around center
        tids: list[int] = []
        for f in range(fingers):
            offset = (f - (fingers - 1) / 2.0) * finger_spacing
            tid = self._client.touch_down(float(from_x), float(from_y + offset))
            tids.append(tid)

        for i in range(1, steps + 1):
            frac = i / steps
            cx = from_x + dx * frac
            cy = from_y + dy * frac
            for f, tid in enumerate(tids):
                offset = (f - (fingers - 1) / 2.0) * finger_spacing
                self._client.touch_move(tid, cx, cy + offset)
            time.sleep(step_delay)

        for tid in tids:
            self._client.touch_up(tid)

    def keyboard_type_unicode(
        self,
        text: str,
        env: dict[str, str] | None = None,
    ) -> bool:
        """Type arbitrary Unicode text using wtype or clipboard fallback.

        Args:
            text: Text to type (supports non-ASCII, e.g. Korean, CJK).
            env: Environment for the spawned tool. MUST contain the session's
                WAYLAND_DISPLAY (and XDG_RUNTIME_DIR) so that wtype/wl-copy
                connect to the isolated compositor instead of the host, plus
                DBUS_SESSION_BUS_ADDRESS. If None, os.environ is used
                (host session only — wtype will target the host compositor).

        Returns:
            True if text was typed successfully.

        Note:
            The AutomationEngine (core.py) passes its ``_session_env()`` here.
            Previously the env was built locally without WAYLAND_DISPLAY,
            which made wtype fail with "Wayland connection failed" in isolated
            sessions and left the clipboard fallback unreachable (H-1).
        """
        if env is None:
            env = dict(__import__("os").environ)

        # Try wtype first. NOTE: kwin_wayland --virtual does NOT expose the
        # zwp_virtual_keyboard_manager_v1 protocol wtype needs ("Compositor
        # does not support the virtual keyboard protocol"), so in virtual
        # sessions this branch always fails and the clipboard paste below is
        # the primary path. It is kept for live sessions where wtype works.
        if shutil.which("wtype"):
            result = subprocess.run(
                ["wtype", "--", text],
                env=env,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
            # wtype failed (e.g. missing virtual-keyboard protocol) — fall
            # through to the clipboard fallback instead of giving up.

        # Clipboard paste via wl-copy + Ctrl+Shift+V, then Ctrl+V.
        # The EIS keyboard is layout-independent for modifier combos, so this
        # is the reliable route for non-ASCII text in virtual sessions where
        # wtype cannot connect.
        # Use Popen + DEVNULL to avoid pipe-blocking from wl-copy's forked child
        if shutil.which("wl-copy"):
            cp = subprocess.Popen(
                ["wl-copy", "--", text],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)  # Wait for fork to complete and offer the selection
            if cp.poll() is None or cp.returncode == 0:
                # Konsole binds paste to Ctrl+Shift+V (its ACCEL convention),
                # most other apps use Ctrl+V. Send both; on apps that bind
                # only one of them the other is a no-op shortcut.
                self.keyboard_key("ctrl+shift+v")
                time.sleep(0.15)
                self.keyboard_key("ctrl+v")
                # Terminators: in a zsh line editor the unbound Ctrl+V above
                # arrives as ^V (quoted-insert) and silently consumes the
                # first Return that follows it, no matter the delay (verified
                # experimentally at 0.1-1.2s gaps). Two Enters guarantee the
                # paste is committed: the first satisfies the quoted-insert,
                # the second executes the pasted line (or is an empty
                # command — harmless in shells and inert in GUI apps).
                time.sleep(0.5)
                self.keyboard_key("return")
                time.sleep(0.3)
                self.keyboard_key("return")
                return True

        return False

    def close(self) -> None:
        """Close the EIS connection."""
        self._client.close()


def _key_name_to_evdev(name: str) -> int | None:
    """Convert a key name to its Linux evdev keycode."""
    lower = name.lower()

    if lower in _EVDEV_KEY_MAP:
        return _EVDEV_KEY_MAP[lower]

    # Single character → look up in character map
    if len(name) == 1:
        entry = _CHAR_KEY_MAP.get(name)
        if entry:
            return entry[0]

    return None


def _resolve_modifiers(modifiers: list[str] | None) -> list[int]:
    """Resolve modifier key names to evdev keycodes."""
    if not modifiers:
        return []
    codes: list[int] = []
    for mod in modifiers:
        code = _MODIFIER_KEYS.get(mod.lower())
        if code is not None:
            codes.append(code)
    return codes


def _parse_key_combo(key: str) -> tuple[list[int], int | None]:
    """Parse a key combo string into (modifier_codes, main_keycode).

    Returns a tuple of (list of modifier evdev keycodes, main key evdev keycode or None).
    """
    parts = key.split("+")
    modifiers: list[int] = []
    main_key: str | None = None

    for part in parts:
        part_lower = part.strip().lower()
        if part_lower in _MODIFIER_KEYS:
            modifiers.append(_MODIFIER_KEYS[part_lower])
        else:
            main_key = part.strip()

    keycode: int | None = None
    if main_key is not None:
        keycode = _key_name_to_evdev(main_key)

    return modifiers, keycode
