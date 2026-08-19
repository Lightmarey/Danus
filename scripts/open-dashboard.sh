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

PROJECT="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
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

if [ -z "$PROJECT" ]; then
  mapfile -t PROJECTS < <("$PYTHON" -c 'from danus.orchestration.cli import do_list; print("\n".join(row["project"] for row in do_list()))')
  [ "${#PROJECTS[@]}" -gt 0 ] || { echo "no Danus projects were found" >&2; exit 1; }
  echo "Danus projects:"
  for i in "${!PROJECTS[@]}"; do printf '  %d) %s\n' "$((i + 1))" "${PROJECTS[$i]}"; done
  while [ -z "$PROJECT" ]; do
    read -r -p "Select project [1-${#PROJECTS[@]}] (default 1): " CHOICE || exit 2
    CHOICE="${CHOICE:-1}"
    case "$CHOICE" in
      *[!0-9]*|"") echo "enter one of the displayed numbers" >&2 ;;
      *)
        if [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le "${#PROJECTS[@]}" ]; then
          PROJECT="${PROJECTS[$((CHOICE - 1))]}"
        else
          echo "enter one of the displayed numbers" >&2
        fi
        ;;
    esac
  done
fi

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
URL="$(printf '%s\n' "$LOGS" | grep -Eo 'http://[^/:[:space:]]+:[0-9]+/#control-token=[^[:space:]]+' | tail -n 1)"
[ -n "$URL" ] || { echo "dashboard started, but its capability URL was not found in logs for dashboard-$PROJECT" >&2; exit 1; }
case "$URL" in
  http://0.0.0.0:*) URL="${URL/http:\/\/0.0.0.0:/http:\/\/$(hostname -I | awk '{print $1}'):}" ;;
esac
URL="${URL/\/#control-token=/\/?launch=$(date +%s)#control-token=}"

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
