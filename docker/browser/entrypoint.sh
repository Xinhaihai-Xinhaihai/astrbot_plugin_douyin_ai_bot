#!/bin/sh
set -eu

: "${VNC_PASSWORD:?VNC_PASSWORD must be set}"
if [ "${#VNC_PASSWORD}" -gt 8 ]; then
    echo "VNC_PASSWORD must contain at most 8 characters" >&2
    exit 1
fi

DISPLAY_NUMBER="${DISPLAY_NUMBER:-:99}"
SCREEN_SIZE="${SCREEN_SIZE:-1600x1000x24}"
PROFILE_DIR="${CHROMIUM_PROFILE:-/home/chromium/profile}"
VNC_PASS_FILE=/tmp/douyin-vnc.pass
XVFB_PID=
X11VNC_PID=
WEBSOCKIFY_PID=
CHROMIUM_PID=

cleanup() {
    for pid in "$CHROMIUM_PID" "$WEBSOCKIFY_PID" "$X11VNC_PID" "$XVFB_PID"; do
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    rm -f "$VNC_PASS_FILE"
}
trap cleanup EXIT INT TERM

mkdir -p "$PROFILE_DIR"
x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASS_FILE" >/dev/null 2>&1

Xvfb "$DISPLAY_NUMBER" -screen 0 "$SCREEN_SIZE" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if xdpyinfo -display "$DISPLAY_NUMBER" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! xdpyinfo -display "$DISPLAY_NUMBER" >/dev/null 2>&1; then
    cat /tmp/xvfb.log >&2
    exit 1
fi

x11vnc -display "$DISPLAY_NUMBER" -forever -shared -rfbauth "$VNC_PASS_FILE" \
    -rfbport 5900 -localhost -noxdamage -repeat -wait 20 >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
WEBSOCKIFY_PID=$!

chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-blink-features=AutomationControlled \
    --disable-features=TranslateUI \
    --no-first-run \
    --no-default-browser-check \
    --password-store=basic \
    --remote-debugging-address=0.0.0.0 \
    --remote-debugging-port=9222 \
    --remote-allow-origins='*' \
    --user-data-dir="$PROFILE_DIR" \
    --window-size=1440,900 \
    --window-position=0,0 \
    about:blank >/tmp/chromium.log 2>&1 &
CHROMIUM_PID=$!

wait "$CHROMIUM_PID"
