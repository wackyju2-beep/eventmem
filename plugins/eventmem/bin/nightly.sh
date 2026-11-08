#!/usr/bin/env bash
# One edge-building pass for eventmem.
#
# Runs `eventmem.nightly` against the local decision service. If the service is not
# healthy it is started once (EVENTMEM_START_SCRIPT) and stopped again on exit.
# Edges are idempotent and the cursor keeps a two-minute overlap, so re-running is safe.
#
# Env: EVENTMEM_PY            python to use (default python3)
#      EVENTMEM_PLUGINS       dir that CONTAINS the eventmem package (default ~/.hermes/plugins)
#      EVENTMEM_JEV_URL       decision service base url (default http://127.0.0.1:18090)
#      EVENTMEM_START_SCRIPT  optional: start the service
#      EVENTMEM_STOP_SCRIPT   optional: stop it again
set -euo pipefail

PY=${EVENTMEM_PY:-python3}
PLUGINS=${EVENTMEM_PLUGINS:-$HOME/.hermes/plugins}
URL=${EVENTMEM_JEV_URL:-http://127.0.0.1:18090}
START=${EVENTMEM_START_SCRIPT:-}
STOP=${EVENTMEM_STOP_SCRIPT:-}

# The decision service must not inherit this shell's PYTHONPATH: a hub site-packages
# ahead of its own venv shadows huggingface_hub and breaks transformers.
unset PYTHONPATH

OWN_SERVICE=0
if ! curl -sf --max-time 2 "$URL/health" >/dev/null 2>&1; then
  if [ -z "$START" ]; then
    echo "eventmem: 决策服务 $URL 没起来，也没配 EVENTMEM_START_SCRIPT" >&2
    exit 1
  fi
  "$START" >/dev/null
  OWN_SERVICE=1
fi

cleanup() {
  if [ "$OWN_SERVICE" = "1" ] && [ -n "$STOP" ]; then
    "$STOP" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$PLUGINS"
PYTHONPATH="${EVENTMEM_PYTHONPATH:+$EVENTMEM_PYTHONPATH:}$PLUGINS" "$PY" -m eventmem.nightly "$@"
