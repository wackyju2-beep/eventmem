#!/usr/bin/env bash
# Build the browser harness for the desktop console plugin.
#
#   ./tools/plugin-smoke/harness/build.sh && python3 -m http.server 8899 -d tools/plugin-smoke/harness
#
# The shipped plugin.js is copied and patched (export the page components), then bundled
# with esbuild against the local react + a stubbed plugin SDK. Never edits the source.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PLUGIN="${1:-$REPO/adapters/hermes/desktop-plugins/eventmem-gui/plugin.js}"
MODULES="${HERMES_NODE_MODULES:-$HOME/.hermes/hermes-agent/node_modules}"
ESBUILD="${ESBUILD:-$MODULES/.bin/esbuild}"

[ -x "$ESBUILD" ] || { echo "missing esbuild at $ESBUILD (set HERMES_NODE_MODULES)" >&2; exit 1; }

python3 - "$PLUGIN" "$HERE/plugin.patched.js" <<'PY'
import pathlib, sys
source, target = sys.argv[1], sys.argv[2]
text = pathlib.Path(source).read_text(encoding="utf-8")
text += "\nexport { Overview, Backends, Models, Schedule, Logs, GraphTab, Console }\n"
pathlib.Path(target).write_text(text, encoding="utf-8")
PY

mkdir -p "$HERE/dist"
"$ESBUILD" "$HERE/entry.mjs" --bundle --format=iife --target=chrome110 \
  --alias:react="$MODULES/react" --alias:react-dom="$MODULES/react-dom" \
  --alias:@hermes/plugin-sdk="$HERE/../sdk-stub/index.mjs" \
  --outfile="$HERE/dist/harness.js" --log-level=warning

echo "built $HERE/dist/harness.js"
