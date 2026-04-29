#!/usr/bin/env bash
# Run each given test with a fresh wcmux server (so state like IP-lockout doesn't bleed).
# Usage: tests/runserver.sh tests/test_m1_auth.py [more...]
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="${WCMUX_TEST_VENV:-/tmp/wcmux-venv}"

if [[ ! -x "$VENV/bin/wcmux" ]]; then
  echo "venv not found at $VENV; run: uv venv $VENV --python 3.10 && VIRTUAL_ENV=$VENV uv pip install --link-mode=copy -e $ROOT" >&2
  exit 2
fi

PORT="${WCMUX_TEST_PORT:-8022}"
LOG="${WCMUX_TEST_LOG:-/tmp/wcmux.log}"

# Isolate the test server from any ambient WCMUX_* (e.g. from the running
# systemd service) so we don't accidentally inherit a hash or host binding.
unset WCMUX_PORT WCMUX_HOST WCMUX_BASE_URL WCMUX_PASSWORD_HASH \
      WCMUX_SHELL WCMUX_SECRET_KEY WCMUX_TRUST_PROXY
export WCMUX_PASSWORD="${WCMUX_PASSWORD:-pw}"
# Per-run device token registry — never touch the real ~/.local/share/wcmux.
export WCMUX_DEVICES_FILE="${WCMUX_DEVICES_FILE:-/tmp/wcmux-test-devices.json}"
rm -f "$WCMUX_DEVICES_FILE" 2>/dev/null || true
# Per-run preview root (spec §4.22) — isolate filesystem view from $HOME.
export WCMUX_PREVIEW_ROOT="${WCMUX_PREVIEW_ROOT:-/tmp/wcmux-test-preview}"
rm -rf "$WCMUX_PREVIEW_ROOT" 2>/dev/null || true
mkdir -p "$WCMUX_PREVIEW_ROOT"
# Extra root for §4.22 multi-root tests.
export WCMUX_PREVIEW_EXTRA_ROOTS="${WCMUX_PREVIEW_EXTRA_ROOTS:-/tmp/wcmux-test-extra}"
for er in $(echo "$WCMUX_PREVIEW_EXTRA_ROOTS" | tr ':' ' '); do
  rm -rf "$er" 2>/dev/null || true
  mkdir -p "$er"
done

unset http_proxy https_proxy

run_one() {
  local test="$1"
  echo "=== $test ==="
  "$VENV/bin/wcmux" --port "$PORT" --host 127.0.0.1 >"$LOG" 2>&1 &
  local PID=$!
  # wait for server to start
  for _ in $(seq 1 20); do
    if curl -sS --noproxy '*' -o /dev/null http://127.0.0.1:$PORT/healthz 2>/dev/null; then break; fi
    sleep 0.2
  done
  local rc=0
  "$VENV/bin/python" "$test" || rc=$?
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  return $rc
}

status=0
for t in "$@"; do
  if ! run_one "$t"; then
    status=1
  fi
done

exit $status
