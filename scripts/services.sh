#!/usr/bin/env bash
# POSIX compatibility entry point; service behavior lives in danus.services.
set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
. "$HERE/scripts/env.sh"
exec "${DANUS_PY:-python}" -m danus.orchestration services "$@"
