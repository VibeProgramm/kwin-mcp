# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Fractional KWin window geometries (e.g. `601.8031365528899` under fractional scaling) were silently dropped by the geometry parser (`int()` raised `ValueError` on the fractional string), so AT-SPI coordinate translation fell back to the `(0, 0)` no-op offset and clicks landed on empty desktop. Geometry components are now parsed as decimals rounded half-up to device pixels, so such windows resolve to their real screen offset. Non-finite tokens (`Infinity`/`-Infinity`/`inf`) raised `OverflowError` out of the parser and discarded the whole payload; such lines are now dropped like other malformed lines while valid neighbours are kept
- A failing `ei_device_unref` inside the EIS device-removal handler aborted the removal halfway (the removed slots stayed half-cleared, the second device in the same drain never processed, touch gestures dangled), unlike the sibling seat-removed/teardown/close paths which tolerate per-slot release failures. The remove path now suppresses per-slot unref failures the same way, so every matching slot is still cleared and the touch cleanup still runs
- The touch-invalidate helper sent a finishing `ei_touch_up` into a REMOVED device before releasing the gestures (observed order: `device_unref` then `touch_up`). Removal now releases the gestures without sending into the removed device (PAUSED keeps the finish + release, as do the teardown/seat-removed paths); on a dead connection no `touch_up` is sent on any path
- The `_RECONNECT_WALL_BUDGET_S` name/comment overstated its guarantee ("overall wall-clock bound for one readiness-gate call") while the budget is only checked between reconnect attempts and an in-flight handshake can run past it. It is renamed to `_RECONNECT_RETRY_BUDGET_S` with the honest semantics documented (reconnect-attempt wall budget; ~10s honest worst case); behavior is unchanged
- A dead EIS socket without a DISCONNECT event (negative `ei_dispatch` return) used to be ignored, leaving the client in "emulating" state and injecting into a void forever; a server DISCONNECT event raised straight out of the event-drain loop, bypassing the recovery path. Both now mark the connection dead and the next injection rebuilds it
- A failed (partial) EIS handshake — e.g. pointer resumed but keyboard never — used to leak the half-initialized connection (started devices, device references, the EI context, a live D-Bus cookie). The failure path now stops started devices, releases everything and resets state before reporting the error; a failed reconnect surfaces as a tool error with context instead of a bare crash
- A second EIS device of the same capability advertised while the current one was alive and emulating replaced it (unref'ing a working device mid-flight); device slots are now only switched when the slot is empty or the current device is paused/removed. Duplicate RESUMED events no longer restart the emulation sequence
- Modifier combos (e.g. alt+F4, ctrl+shift+t) were spread across several EIS frames with per-key delays, which let KWin pause the devices mid-combo and drop the remaining strokes. Combo key presses are now batched into a single EIS frame (keyboard_burst) with a 20ms gap before the batched release frame; the ctrl+q dual-binding alias still sends its two combos as separate sequential burst pairs, never merged into one frame (adopted from 01SW/kwin-mcp)
- Active touch sequences survived an EIS reconnect with pointers into the released EI context (use-after-free on the next touch_move/touch_up); a reconnect now finishes and releases active touches and resets touch IDs
- A released EI context (`_ei == 0`, e.g. left by a failed reconnect) reached libei's `ei_get_fd`/`ei_dispatch` on the next injection — a NULL dereference that segfaults the whole MCP server (libei 1.6.0). A released context is now treated as a dead connection: the readiness wait short-circuits to the reconnect path and the failure surfaces as a tool error, every time
- `touch_move`/`touch_up` fetched the touch pointer from the active-touch table *before* the device-readiness wait, so a reconnect inside that wait unref'd the touch under the stale pointer (use-after-free). Both methods now wait first and re-fetch after; a gesture finished by the recovery path reports "No active touch" instead of touching released memory
- The touch device being PAUSED or REMOVED (or the seat removed) now finishes and forgets active touch gestures: per libei, pausing resets the device's logical state to neutral ("any touches logically down are released"), so a surviving gesture ID referenced a server-side gesture that no longer exists. A pause on the pointer device leaves touch gestures alone
- `_reconnect` duplicated the handshake teardown with fewer guards (no stop-emulating before device release, no cookie reset on a failed disconnect, no suppression on the touch cleanup — a single cleanup failure aborted the recovery halfway). It now reuses the shared teardown, so a rebuild can never leak a half-released connection
- `session_start`/`session_connect` hard-failed with a ToolError when the EIS device negotiation timed out (ToolError does not derive from RuntimeError), breaking the established degradation contract; both now degrade to "no input backend" (session_start) / ydotool fallback (session_connect) with the negotiation failure logged, matching the 0.8.1 behaviour
- Text and touch injections gated only on pointer + keyboard readiness, so a paused text/touch device silently dropped input; `text_keysym`/`text_utf8` now additionally wait for the text device and `touch_down`/`touch_move`/`touch_up` for the touch device (zero slot = fallback, skipped)
- Modifier presses around `mouse_click`/`mouse_drag` and held keys in `keyboard_key_down`/`keyboard_key_up` were spread over one EIS frame per key, letting KWin pause mid-press; all modifier press/release sets are now single `keyboard_burst` frames like the combo path
- kscreen-doctor flag lines combining several flags (`enabled connected priority 1`) were not recognised (only a bare `enabled` line matched), so such outputs were skipped as disabled; flag lines are now token-parsed with priority extracted from any flag line
- The inbound event drain (`ei_get_event` → `_handle_event` → unref) was triplicated across the handshake, the readiness wait and the post-send flush; all three share a `_drain_events` helper and the device-slot tuple lives in one `_DEVICE_ATTRS` constant
- A multi-capability EIS handle occupying several device slots leaked one `ei_device_ref` per extra slot on remove/seat-removal/teardown (two refs taken, one released), and `close()` stopped and unref'd the shared handle twice while leaving an aliased touch slot pointing at the freed handle. Ownership is now uniformly per slot on every path (one ref taken and released per occupied slot; emulation still stopped once per handle), and `close()` reuses the shared teardown, so every cleanup step is individually failure-tolerant instead of aborting on the first error
- The EIS device-readiness wait probed the stale emulation set right after draining a queued DISCONNECT event, reporting ready on a dead connection so the injection ran into it instead of rebuilding. A dead connection observed after the drain (or at the deadline) now fails the wait, so the caller takes the reconnect path
- `hold_keys`/`hold_button` recorded the held intent only after the post-send `_flush` returned, so a `PAUSED`→`RESUMED` drained inside the same call found a stale (empty) held set: the hold missed its replay (client held, server neutral — silent divergence). Conversely `release_keys`/`release_button` dropped the intent after the flush, so the same drain re-pressed the just-released key/button on the wire (sticky modifier). The intent is now recorded/dropped before the send and rolled back on a send failure (`ToolError`), so same-call recovery replays holds and never replays releases while a failed injection leaves the held sets untouched

### Changed

- **BREAKING**: `session_start` accepts `screen_width=0`/`screen_height=0` (now the default) to match the virtual screen to the visible (logical) desktop, detected at every call via kscreen-doctor (bounding box of the logical desktop — the union of enabled outputs' `Geometry: X,Y WxH` rectangles; ANSI-coloured output is stripped, disabled outputs skipped, mirrored outputs collapse) with an xrandr fallback (the union of connected monitors' `WxH+X+Y` rectangles, then the Screen current size) and a 1920x1080 last resort. 0 in either dimension auto-detects both dimensions (no mixed detected/explicit sizes). Clients relying on the old fixed default get an auto-detected size; **pass explicit `screen_width`/`screen_height` to keep the previous behaviour** (adopted from 01SW/kwin-mcp)
- The MCP `serverInfo` now carries the installed package version (previously an empty string)
- `_flush` on a released EIS context (`_ei == 0`) now raises a clean ToolError instead of returning silently: the old path reported success for an injection that never reached libei, against the honest-delivery contract
- The inbound event drain now stops after a DISCONNECT event: events still queued behind it (e.g. a RESUMED) belong to the dead connection and no longer trigger `start_emulating` or held-state replay on it; a handshake interrupted by DISCONNECT/dispatch failure now fails instead of reporting success on stale device handles
- The EIS reconnect retry loop is additionally bounded by a `_RECONNECT_RETRY_BUDGET_S` (4s) wall-clock deadline measured from the readiness-gate entry: the attempt count alone did not bound one call (a wedged handshake can sit out its 5s deadline per attempt). The exhaustion ToolError names the attempts actually made. Honest worst case per call is now ~10s (stall wait + budget + one in-flight handshake + re-check); in the live cycle (fast handshakes) recovery still lands in ~3-4s

## [0.8.2] - 2026-09-07

### Fixed

- The last-resort fallback of the at-spi-bus-launcher resolver returned `candidates[0]` (`/usr/libexec/...`, the Debian/Ubuntu/Fedora layout) when no candidate file existed and `shutil.which` found nothing — a dead path on Arch. The fallback is now the Arch default `/usr/lib/at-spi-bus-launcher`, and the resolved path is shell-quoted when embedded into the session's bash wrapper
- The reason an `InputBackend` (KWin EIS) setup failed was silently swallowed when `session_start`/`session_connect` degraded to "no input backend"/ydotool; the exception text is now logged as a warning before the degradation (backend selection unchanged)

### Internal

- Test cleanups (dead assignment, tautological assert) and type hints on test fakes/helpers

## [0.8.1] - 2026-09-07

### Fixed

- `session_start`/`session_connect` crashed entirely when KWin's EIS D-Bus interface was unavailable: `EISClient._setup` left `get_object`/`Interface`/`connectToEIS` unprotected, and `dbus.DBusException` is not a `RuntimeError` (the only type core.py catches to degrade to "no input backend"). The dbus block now raises `RuntimeError("KWin EIS interface unavailable: {exc}")` with error chaining (adopted from upstream isac322/kwin-mcp#42, fix e)
- The bash wrapper hardcoded the Arch-only `/usr/lib/at-spi-bus-launcher` path: on Debian/Ubuntu/Fedora the binary lives in `/usr/libexec` (or `/usr/lib/at-spi2-core`), so the launcher silently no-oped and the session's accessibility bus was dead. The path is now resolved on the Python side before the wrapper is assembled — first existing candidate from `/usr/libexec`, `/usr/lib`, `/usr/lib/at-spi2-core`, then `shutil.which`, then the Arch default (adopted from upstream isac322/kwin-mcp#42, fix c)
- `capture_screenshot_to_file` unconditionally invoked the spectacle CLI, contrary to its own documentation, and minimal/virtual sessions may not have spectacle installed at all. It now tries the ScreenShot2 D-Bus capture first and falls back to spectacle; when both fail, the error carries both causes. The shared single-frame helper drains the pixel pipe concurrently with the D-Bus call (KWin streams pixels before replying) and carries a 5s timeout instead of dbus-python's 25s default (adopted from upstream isac322/kwin-mcp#42, fix f)
- `session_start` could hang forever when the KWin wrapper never printed `READY` (dead KWin, missing binaries) or waited forever for the Wayland socket: the parent now reads the wrapper's stdout with a 25s deadline, and the wrapper's socket wait is bounded (150 x 0.1s = 15s, reports `NOSOCKET` and exits 1). Startup failures now include KWin's stderr, and `launch_app` no longer leaks the host `DISPLAY` into isolated sessions, where X11 applications would silently open on the user's real desktop (adopted from upstream isac322/kwin-mcp#50)
- EIS input injection started emulating before the compositor had resumed the devices, which libei rejects (`device is not emulating`) and which silently dropped every injected event: `_negotiate_devices` now waits for `EI_EVENT_DEVICE_RESUMED` on the pointer and keyboard before `ei_device_start_emulating` and fails with an explicit error if a device never resumes (adopted from upstream isac322/kwin-mcp#42)
- Segfault on Python 3.14 caused by missing `argtypes` on variadic `ei_seat_bind_capabilities` ctypes call
- `accessibility_tree` / `find_ui_elements` returned window-local coordinates instead of true screen coordinates, so clicks computed from them landed on empty desktop while keyboard input kept working. Coordinates are now translated by each window's compositor-side client origin (queried from KWin via a one-shot script, matched by caption, best-effort with fallback to untranslated coordinates)
- The `keyboard_key` ctrl+q alias sent only Ctrl+Shift+Q (Konsole's close-window binding), so apps binding the conventional plain Ctrl+Q (kcalc, kwrite) could never be closed by the alias; it now sends both combos sequentially with a short pause (plain Ctrl+Q first, then Ctrl+Shift+Q) — on apps that bind only one of them the other is an inert no-op shortcut, mirroring the paste-alias pattern
- `dbus_call` returned D-Bus failures (ServiceUnknown, UnknownMethod, missing `dbus-send` binary) as success-payload strings with `isError=false`, so agents could not tell a failure from a reply; anticipated D-Bus failures now raise ToolError (`isError=true` carrying the message), matching the read_app_log/clipboard error contract

## [0.8.0] - 2026-09-06

### Changed

- Migrated from MCP Python SDK 1.x to 2.x: `FastMCP` renamed to `MCPServer` (`from mcp.server import MCPServer`). The tool API is unchanged — all 30 tools keep their names, parameters, descriptions, and behavior
- This release also includes fixes already merged into the fork's main after v0.7.0: true screen
  coordinates for `accessibility_tree` / `find_ui_elements` (translated via KWin window geometry)
  and the Python 3.14 segfault fix in the libei variadic call
- Tool handlers in `server.py` are now declared `async def`: mcp 2.x runs synchronous handlers on anyio worker threads, which crashed libei/D-Bus ctypes usage (SIGSEGV in `_bind_seat_capabilities`). `async def` keeps handlers on the event loop thread, matching v1 behavior
- Dependencies upgraded to the latest releases: `mcp` 1.26.0 → 2.1.1, `Pillow` 12.1.1 → 12.3.0, `PyGObject` 3.54.5 → 3.58.0, `anyio` 4.12.1 → 4.15.1, `pydantic` 2.12.5 → 2.13.5, `starlette` 0.52.1 → 1.6.0, `uvicorn` 0.41.0 → 0.52.4 (new transitive dependencies of `mcp` 2.x: `mcp-types` 2.1.1, `httpx2` 2.12.0, `opentelemetry-api` 1.44.0)
- `mcp` dependency constraint tightened from `>=1.0.0` (unbounded) to `>=2.0.0,<3` to prevent silent major-version drift from repeating the v1→v2 incident where an unbounded bound resolved to a new major and broke fresh launches
- Minimum dependency floors raised in `pyproject.toml`: `PyGObject>=3.58.0`, `dbus-python>=1.4.0`, `Pillow>=12.3.0`

## [0.7.0] - 2026-03-29

### Added

- `session_connect` tool for attaching to an existing KWin session (real desktop or container) instead of creating an isolated virtual one. Defaults to `$DBUS_SESSION_BUS_ADDRESS` and `$WAYLAND_DISPLAY` from the environment. Clipboard is always enabled for live sessions.
- `--default-live-session` flag for both MCP server (`kwin-mcp`) and CLI (`kwin-mcp-cli`) to switch the default session mode from virtual to live. When active, `session_connect` becomes the recommended tool and `session_start` requires explicit invocation.
- `LiveSession` class in `session.py` for managing connections to existing KWin compositors without lifecycle management
- `SessionType` enum (`VIRTUAL` / `LIVE`) and `session_type` field on `SessionInfo` for distinguishing session types

### Changed

- `session_stop` now only disconnects (without killing KWin or pre-existing apps) when used with live sessions
- Error messages for missing sessions now mention both `session_start` and `session_connect`
- Clipboard error messages now mention `session_connect` as an alternative (clipboard is always enabled for live sessions)
- Tool count increased from 29 to 30

## [0.6.0] - 2026-02-25

### Added

- `isolate_home` option in `session_start` to create a temporary HOME directory with isolated XDG directories (`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`), preventing apps from reading/writing host user settings
- `keep_home` option in `session_start` to preserve the isolated home directory after `session_stop`, useful for inspecting app-generated config/data files
- `list_windows` now shows per-window titles and `[active]`/`[focused]` state markers using AT-SPI2 `ACTIVE`/`FOCUSED` states
- `states` parameter for `find_ui_elements` to filter elements by AT-SPI2 states (e.g. `["focused"]`, `["active", "visible"]`). Query can be empty when filtering by states only.
- `expected_states` parameter for `wait_for_element` to wait until elements have specific AT-SPI2 states (e.g. wait for a window to become `["active"]`)
- `role` parameter for `accessibility_tree` to filter the tree to specific element types (e.g. `"button"`, `"check box"`). Non-matching elements are hidden but their children are still traversed.

### Changed

- AT-SPI2 subprocess queries (`_run_atspi`) now retry once on failure with a 0.5s delay, improving resilience against transient AT-SPI2 bus instability
- `find_ui_elements` and `wait_for_element` result messages now include a descriptive search summary with all filter criteria (query, states)

## [0.5.1] - 2026-02-23

### Fixed

- `session_start` `screen_width`/`screen_height` parameters were being ignored — now correctly passed as `--width`/`--height` flags to `kwin_wayland`

### Added

- `keep_screenshots` option in `session_start` to preserve screenshot files after `session_stop`, useful for debugging and CI artifact collection
- SEO documentation guidelines in `CLAUDE.md`, `docs-seo` custom agent, `release-notes` skill, GitHub issue/PR templates, and `CONTRIBUTING.md`

## [0.5.0] - 2026-02-23

### Added

- **`AutomationEngine` (`core.py`)**: MCP-independent automation logic extracted from `server.py` into a standalone reusable class covering session, input, screenshot, and accessibility operations
- **Interactive CLI (`kwin-mcp-cli`)**: New entry point with REPL and pipe mode for testing all 29 tools without an MCP client

### Changed

- `server.py` simplified to thin MCP wrappers delegating to `AutomationEngine`
- Improved AT-SPI2 bus address propagation and reduced launcher sleep time

## [0.4.2] - 2026-02-22

### Changed

- Added JSON Schema `description` fields to all parameters across all 29 MCP tools for improved discoverability and client-side documentation
- Rewrote `README.md` with complete tool reference tables, architecture diagram, and SEO-optimized metadata

## [0.4.1] - 2026-02-22

### Fixed

- Explicitly pass `KWIN_WAYLAND_NO_PERMISSION_CHECKS` and `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS` env vars directly to the KWin process in the wrapper script — environment inheritance through `dbus-run-session` was unreliable, causing restricted Wayland protocols (e.g. `org_kde_plasma_window_management`) and `X-KDE-Wayland-Interfaces` desktop file declarations to not take effect

## [0.4.0] - 2026-02-22

### Added

- **Restricted Wayland protocol access**: Set `KWIN_WAYLAND_NO_PERMISSION_CHECKS=1` in isolated sessions, enabling clients to bind `org_kde_plasma_window_management` and other KWin-restricted protocols — critical for testing apps that use Plasma's TasksModel / window management APIs
- **App stdout/stderr capture**: `launch_app` and `session_start` now redirect app output to per-app log files, with a new `read_app_log` MCP tool to retrieve logs by PID
- **Wayland protocol diagnostics**: New `wayland_info` MCP tool runs `wayland-info` inside the session to enumerate exposed Wayland globals (useful for verifying protocol availability)
- **Environment variable passthrough**: `session_start` and `launch_app` now accept an `env` parameter for passing extra environment variables to launched apps
- **Shell-aware command parsing**: Commands are now parsed with `shlex.split()` instead of `str.split()`, correctly handling quoted arguments (e.g. `bash -c 'echo hello world'`)

### Changed

- `Session.launch_app()` now returns `AppInfo` (with pid, command, log_path) instead of a bare `int` PID
- `SessionInfo` now tracks all launched apps via an `apps` dict keyed by PID

## [0.3.0] - 2026-02-22

### Added

- M5.1 E2E input features: touch input (tap, swipe, pinch, multi-finger swipe), clipboard (get/set), Unicode text input (wtype/wl-copy fallback), window management (launch_app, list_windows, focus_window), `dbus_call`, and `wait_for_element` — 17 new MCP tools total

### Fixed

- External binary missing errors now return helpful install instructions instead of raw `FileNotFoundError` (affects `wl-clipboard`, `wtype`, `dbus-send`, `spectacle`)

## [0.2.0] - 2026-02-20

### Added

- **Composite frame capture**: Action tools (`mouse_click`, `mouse_move`, `mouse_drag`, `keyboard_type`, `keyboard_key`) now accept an optional `screenshot_after_ms` parameter to capture screenshots at specified delays (in milliseconds) after the action completes
- Fast D-Bus screenshot capture via KWin ScreenShot2 interface (~30-70ms per frame vs ~200-300ms with spectacle CLI)
- Optimized burst capture with two-phase pipeline: raw frame capture with accurate timing, then deferred PNG encoding
- `KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1` environment variable automatically set for isolated sessions to enable direct D-Bus screenshot access

## [0.1.0] - 2026-02-20

### Added

- Isolated KWin Wayland session management (`session_start`, `session_stop`)
- Screenshot capture via KWin's ScreenShot2 D-Bus interface
- Accessibility tree inspection using AT-SPI2
- UI element search by name, role, or description
- Mouse input: click, move, scroll, drag via KWin EIS (Emulated Input Server)
- Keyboard input: text typing and key combinations via KWin EIS
- FastMCP-based MCP server with stdio transport

[Unreleased]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/VibeProgramm/kwin-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/VibeProgramm/kwin-mcp/releases/tag/v0.1.0
