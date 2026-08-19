#!/usr/bin/env bash
# Read-only dashboard for one project (fact graph DAG + global memory + spend).
#   bash scripts/start-dashboard.sh <project_name> [port]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
NAME="${1:?usage: start-dashboard.sh <project_name> [port]}"
PORT="${2:-$DASHBOARD_PORT}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PROJ="$DANUS_AGENTS_ROOT/$NAME"
[ -d "$PROJ" ] || { echo "no such project: $PROJ" >&2; exit 1; }
echo "[dashboard] http://$HOST:$PORT  project=$NAME"
exec "$DANUS_PY" -m danus.observability --project "$PROJ" --host "$HOST" --port "$PORT"
