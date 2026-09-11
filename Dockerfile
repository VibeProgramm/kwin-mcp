# Glama handshake image (not a runtime image for real desktop automation).
#
# Purpose: pass Glama's "Build test -> Make Release" flow (build container,
# start server, MCP initialize + tools/list over stdio, security scans).
# The server starts and answers the MCP handshake WITHOUT a running KWin
# compositor, AT-SPI bus, or Wayland display: AutomationEngine.__init__
# (src/kwin_mcp/core.py) only sets None/False, and kwin_wayland / spectacle /
# libei / Atspi / wl-copy / wtype are all touched lazily at tool-call time.
# Tool calls inside this container return "No active session" by design —
# real GUI automation needs a KDE Plasma 6 Wayland host (see README.md).
#
# Build + handshake test (podman or docker):
#   podman build -t kwin-mcp-glama .
#   printf '%s' '{"jsonrpc":"2.0","id":1,"method":"initialize",
#     "params":{"protocolVersion":"2024-11-05","capabilities":{},
#     "clientInfo":{"name":"glama-check","version":"0"}}}' \
#     | podman run --rm -i kwin-mcp-glama

FROM python:3.12-slim

COPY . /app
WORKDIR /app

# Runtime libs for eager imports: dbus-python (core.py, input.py,
# screenshot.py) needs libdbus-1-3 + libglib2.0-0 (+ libcairo2 for pycairo);
# the process-wide AT-SPI2 subprocess (-m kwin_mcp.accessibility ->
# gi.require_version("Atspi")) needs libgirepository + the Atspi typelib.
# libei1 is present so the lazy ctypes.CDLL("libei.so.1") lookup resolves;
# it is never opened at startup. The -dev/build-essential packages are only
# needed to compile PyGObject/dbus-python wheels and are purged in the same
# layer. No KWin/Plasma/spectacle: GBs of GUI stack + GPU/DRM for zero
# handshake gain.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libdbus-1-3 \
    libglib2.0-0 \
    libcairo2 \
    libgirepository-1.0-1 \
    gir1.2-atspi-2.0 \
    at-spi2-core \
    libei1 \
    libcairo2-dev \
    libgirepository-2.0-dev \
    libdbus-1-dev \
    pkg-config \
    build-essential \
    && pip install --no-cache-dir . \
    && apt-get purge -y --auto-remove \
    libcairo2-dev \
    libgirepository-2.0-dev \
    libdbus-1-dev \
    pkg-config \
    build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache

CMD ["kwin-mcp"]
