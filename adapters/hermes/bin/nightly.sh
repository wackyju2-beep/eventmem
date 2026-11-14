#!/usr/bin/env bash
# One nightly edge-building pass for eventmem.
#
# Starts the local decision service if it is not already healthy, runs the CLI, and
# stops the service again only if this script started it.  Edges are idempotent and the
# cursor has a two-minute overlap, so a re-run is harmless.
#
# Thresholds come from ~/.hermes/config.yaml (plugins.entries.eventmem.settings) — this
# script only decides which interpreter and when to start/stop the model service.
set -euo pipefail

# Resolve the symlink: the plugin directory under ~/.hermes/plugins is a link.
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../.." && pwd)"

PY="${EVENTMEM_PY:-}"
if [ -z "$PY" ]; then
  PY="$HOME/.hermes/hermes-agent/.venv/bin/python"
  for candidate in "$HOME"/.hermes/installs/*/environments/*/venv/bin/python; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
fi

# The decision service must not see this shell's PYTHONPATH: a hub site-packages
# ahead of its own venv shadows huggingface_hub and breaks transformers.
unset PYTHONPATH

START_SCRIPT="${EVENTMEM_START_SCRIPT:-}"
URL="${EVENTMEM_JEV_URL:-http://127.0.0.1:18090}"

OWN_SERVICE=0
if ! curl -sf --max-time 2 "$URL/health" >/dev/null 2>&1; then
  if [ -x "$START_SCRIPT" ]; then
    "$START_SCRIPT" >/dev/null
    OWN_SERVICE=1
  fi
fi

cleanup() {
  if [ "$OWN_SERVICE" = "1" ]; then
    "${START_SCRIPT%/*}/stop.sh" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# No ``exec``: the EXIT trap must survive to stop a service this script started.
"$PY" "$REPO/adapters/hermes/run.py" nightly "$@"
