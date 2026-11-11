#!/usr/bin/env bash
# Smoke-render the desktop console plugin outside the app.
#
# Runtime plugins are evaluated from blob: URLs with a rewritten import map, so nothing
# here needs Vite — but it does need react + react-dom, which live in the Hermes install.
#
#   ./tools/plugin-smoke/run.sh            # eventmem-gui
#
# What it proves: every tab of the page renders (empty *and* canned payloads) through
# react-dom/server without throwing. It cannot prove that hooks fire or that the IPC
# transport answers — those need the app itself.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PLUGIN="${1:-$REPO/adapters/hermes/desktop-plugins/eventmem-gui/plugin.js}"
MODULES="${HERMES_NODE_MODULES:-$HOME/.hermes/hermes-agent/node_modules}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for pkg in react react-dom; do
  [ -d "$MODULES/$pkg" ] || { echo "missing $MODULES/$pkg — set HERMES_NODE_MODULES" >&2; exit 1; }
  ln -s "$MODULES/$pkg" "$WORK/node_modules_$pkg"
done
mkdir -p "$WORK/node_modules/@hermes/plugin-sdk"
mv "$WORK/node_modules_react" "$WORK/node_modules/react"
mv "$WORK/node_modules_react-dom" "$WORK/node_modules/react-dom"
cp "$HERE/sdk-stub/index.mjs" "$WORK/node_modules/@hermes/plugin-sdk/index.mjs"
cp "$HERE/sdk-stub/package.json" "$WORK/node_modules/@hermes/plugin-sdk/package.json"
echo '{ "type": "module" }' > "$WORK/package.json"

# The harness needs the tab components and a way to seed state (SSR never runs effects),
# so the shipped source is copied with two mechanical patches. Never the file itself.
python3 - "$PLUGIN" "$WORK/plugin.js" <<'PY'
import pathlib
import sys

source, target = sys.argv[1], sys.argv[2]
text = pathlib.Path(source).read_text(encoding="utf-8")
seed = (
    "const __can = (key) => {\n"
    "  const c = globalThis.__canned || {}\n"
    "  const list = String(key).split('|').filter(Boolean)\n"
    "  if (list.length === 1) return c[list[0]] ?? null\n"
    "  return list.every((p) => c[p]) ? list.map((p) => c[p]) : null\n"
    "}\n\n"
)
text = text.replace("const NI = (v) =>", seed + "const NI = (v) =>", 1)
text = text.replace("useState({ data: null, error: '' })", "useState({ data: __can(path), error: '' })", 1)
text = text.replace("useState({ data: null, error: '' })", "useState({ data: __can(key), error: '' })", 1)
text += "\nexport { Overview, Backends, Models, Schedule, Logs, GraphTab, Console }\n"
pathlib.Path(target).write_text(text, encoding="utf-8")
PY

cp "$HERE/run.mjs" "$WORK/run.mjs"

# The English dictionary lives in Python (one source for both UIs); hand it to the harness.
python3 - "$REPO" "$WORK/map.json" <<'PY'
import json
import pathlib
import sys

repo, target = sys.argv[1], sys.argv[2]
sys.path.insert(0, str(pathlib.Path(repo) / "core"))
from eventmem_core.ui_strings import EN  # noqa: E402

pathlib.Path(target).write_text(json.dumps(EN, ensure_ascii=False), encoding="utf-8")
PY

cd "$WORK"
node run.mjs

# Second target: the graph component alone. It is headless (no route, no chip), so a broken
# import there is invisible until the 图谱 tab is opened — this renders it with 0 and 2 atoms.
python3 - "$REPO/adapters/hermes/desktop-plugins/eventmem-graph/plugin.js" "$WORK/graph.js" <<'PY'
import pathlib
import sys

source, target = sys.argv[1], sys.argv[2]
text = pathlib.Path(source).read_text(encoding="utf-8")
text += "\nexport { GraphPage }\n"
pathlib.Path(target).write_text(text, encoding="utf-8")
PY

cp "$HERE/run-graph.mjs" "$WORK/run-graph.mjs"
node run-graph.mjs
