#!/usr/bin/env bash
# Start one dashboard through the native service manager and open its
# ephemeral capability URL. Usage: open-dashboard.sh PROJECT [PORT] [--no-open]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [ -n "${DANUS_PYTHON_BIN:-}" ]; then
  PYTHON="$DANUS_PYTHON_BIN"
elif [ -n "${DANUS_PY:-}" ]; then
  PYTHON="$DANUS_PY"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  PYTHON="$(command -v python)"
fi

PROJECT="${1:?usage: open-dashboard.sh PROJECT [PORT] [--no-open]}"
shift
NO_OPEN=0
if [ "${1:-}" != "" ] && [ "${1:-}" != "--no-open" ]; then
  DASHBOARD_PORT="$1"
  export DASHBOARD_PORT
  shift
fi
if [ "${1:-}" = "--no-open" ]; then
  NO_OPEN=1
  shift
fi
[ "$#" -eq 0 ] || { echo "usage: open-dashboard.sh PROJECT [PORT] [--no-open]" >&2; exit 2; }

case "$PROJECT" in
  ""|*[!A-Za-z0-9._-]*|[-._]*) echo "invalid project name: $PROJECT" >&2; exit 2 ;;
esac
case "${DASHBOARD_PORT:-8099}" in
  *[!0-9]*|"") echo "invalid dashboard port: ${DASHBOARD_PORT:-}" >&2; exit 2 ;;
esac
[ "${DASHBOARD_PORT:-8099}" -ge 1 ] && [ "${DASHBOARD_PORT:-8099}" -le 65535 ] || {
  echo "invalid dashboard port: ${DASHBOARD_PORT:-}" >&2
  exit 2
}

cd "$ROOT"
"$PYTHON" -m danus.orchestration services up dashboard "$PROJECT"
LOGS="$("$PYTHON" -m danus.orchestration services logs "dashboard-$PROJECT")"
URL="$(printf '%s\n' "$LOGS" | grep -Eo 'http://127\.0\.0\.1:[0-9]+/#control-token=[^[:space:]]+' | tail -n 1)"
[ -n "$URL" ] || { echo "dashboard started, but its capability URL was not found in logs for dashboard-$PROJECT" >&2; exit 1; }

printf '%s\n' "$URL"
if [ "$NO_OPEN" -eq 0 ]; then
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$URL"
  else
    echo "no browser opener found; open the URL above" >&2
  fi
fi
