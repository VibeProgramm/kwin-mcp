"""Tests for fractional KWin window geometries (issue #17).

Under fractional scaling KWin reports subpixel (fractional) client/frame
geometries, e.g. ``601.8031365528899``. The parser must accept them
(round-half-up to int) so that ``get_window_geometries`` returns such
windows and ``resolve_offset`` yields the real screen offset instead of
the ``(0, 0)`` no-op fallback.
"""

from __future__ import annotations

from kwin_mcp.kwin_windows import WindowGeometry, _parse_payload, resolve_offset

# Exact live probe line from the issue: KCalc on a fractionally scaled desktop.
FRACTIONAL_PAYLOAD = (
    "OK\n"
    "KCalc\t"
    "601.8031365528899,160.8031365528899,440.9090909090911,553.6363636363636\t"
    "601.8031365528899,188.1437101908282,440.9090909090911,526.3636363636363\t"
    "org.kde.kcalc"
)


def test_parse_fractional_geometry_rounds_half_up() -> None:
    """Fractional KWin geometries parse with round-half-up, not truncation."""
    geometries = _parse_payload(FRACTIONAL_PAYLOAD)
    assert len(geometries) == 1
    geom = geometries[0]
    assert geom.caption == "KCalc"
    assert geom.resource_class == "org.kde.kcalc"
    # 601.80... -> 602 (truncation would give 601).
    assert (geom.client_x, geom.client_y) == (602, 188)
    assert (geom.client_w, geom.client_h) == (441, 526)
    assert (geom.frame_x, geom.frame_y) == (602, 161)
    assert (geom.frame_w, geom.frame_h) == (441, 554)


def test_resolve_offset_fractional_window_end_to_end() -> None:
    """A fractionally reported window yields its real offset, not (0, 0)."""
    geometries = _parse_payload(FRACTIONAL_PAYLOAD)
    assert resolve_offset(geometries, "kcalc", "KCalc", 0, 0) == (602, 188)


def test_parse_integral_geometry_unchanged() -> None:
    """Integral geometries (e.g. plasmashell) keep parsing exactly as before."""
    payload = "OK\nplasmashell\t0,0,1746,30\t0,0,1746,30\torg.kde.plasmashell"
    geometries = _parse_payload(payload)
    assert len(geometries) == 1
    geom = geometries[0]
    assert (geom.client_x, geom.client_y, geom.client_w, geom.client_h) == (0, 0, 1746, 30)
    assert (geom.frame_x, geom.frame_y, geom.frame_w, geom.frame_h) == (0, 0, 1746, 30)


def test_parse_mixed_payload_keeps_fractional_and_integral() -> None:
    """A payload mixing integral and fractional lines keeps both windows."""
    payload = (
        "OK\n"
        "plasmashell\t0,0,1746,30\t0,0,1746,30\torg.kde.plasmashell\n"
        + FRACTIONAL_PAYLOAD.splitlines()[1]
    )
    geometries = _parse_payload(payload)
    assert [g.caption for g in geometries] == ["plasmashell", "KCalc"]


def test_parse_malformed_lines_still_dropped() -> None:
    """Non-numeric geometry fields are still dropped (best-effort contract)."""
    payload = "OK\nBroken\tnotanumber,0,10,10\t0,0,10,10\torg.example"
    assert _parse_payload(payload) == []
    assert _parse_payload("ERROR boom") == []


def test_resolve_offset_unknown_window_still_zero() -> None:
    """Unmatched windows still fall back to the (0, 0) no-op offset."""
    geometries = _parse_payload(FRACTIONAL_PAYLOAD)
    assert resolve_offset(geometries, "kcalc", "NoSuchWindow", 0, 0) == (0, 0)
    assert resolve_offset([], "kcalc", "KCalc", 0, 0) == (0, 0)
    assert resolve_offset(
        [WindowGeometry("KCalc", "org.kde.kcalc", 602, 188, 441, 526, 602, 161, 441, 554)],
        "kcalc",
        "KCalc",
        10,
        20,
    ) == (592, 168)
