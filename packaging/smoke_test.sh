#!/usr/bin/env bash
#
# Smoke-test a built Mini Search desktop artifact: start it, wait for /health,
# fetch the public page, then stop it and confirm the port is released.
#
# Usage:  bash packaging/smoke_test.sh <path-to-binary-or-AppImage>
#
# The server is started with a throwaway MINISEARCH_DATA_DIR so nothing outside
# a temp directory is touched.
set -euo pipefail

TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "Použití: $0 <binárka|AppImage>" >&2; exit 2; }
[ -e "$TARGET" ] || { echo "Nenalezeno: $TARGET" >&2; exit 1; }

PORT="${MINISEARCH_SMOKE_PORT:-8091}"
DATA_DIR="$(mktemp -d)"
export MINISEARCH_DATA_DIR="$DATA_DIR"

# AppImages need extraction when there is no FUSE device (containers, CI).
EXTRA=()
case "$TARGET" in
    *.AppImage) EXTRA=(--appimage-extract-and-run) ;;
esac

echo "==> Spouštím $TARGET na portu $PORT (data: $DATA_DIR)"
# Own process group: the AppImage extract-and-run wrapper forks a child, so the
# whole group must be signalled for the server to actually stop.
setsid "$TARGET" "${EXTRA[@]}" --no-ui --port "$PORT" > "$DATA_DIR/server.log" 2>&1 &
PID=$!

stop_target() {
    # Signal every process that belongs to this run: the direct child's group
    # (if any) plus anything whose command line carries our port. The AppImage
    # runtime forks, so matching the port is what reliably reaches the server.
    # PID=0 means "already stopped": "kill -0" would hit the whole group.
    if [ "${PID:-0}" -gt 0 ] 2>/dev/null; then
        kill -TERM "-$PID" 2>/dev/null || true
    fi
    pkill -TERM -f -- "--port $PORT" 2>/dev/null || true
}

cleanup() {
    stop_target
    rm -rf "$DATA_DIR"
}
trap cleanup EXIT

code="000"
for _ in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
    [ "$code" = "200" ] && break
    sleep 2
done
if [ "$code" != "200" ]; then
    echo "FAIL: /health neodpověděl 200 (naposledy $code)"; tail -20 "$DATA_DIR/server.log"; exit 1
fi
echo "PASS: /health = 200"

curl -sf "http://127.0.0.1:$PORT/" > /dev/null && echo "PASS: veřejná stránka se načte"
curl -sf "http://127.0.0.1:$PORT/admin" > /dev/null && echo "PASS: administrace se načte"

stop_target
# Graceful shutdown stops workers, the scheduler and the DB (checkpoint + close),
# which can take a little while, so allow up to 40 s for the port to free up.
for _ in $(seq 1 40); do
    sleep 1
    if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; then
        echo "PASS: port se po zastavení uvolnil"; PID=0; exit 0
    fi
done
echo "FAIL: server po zastavení stále odpovídá"; PID=0; exit 1
