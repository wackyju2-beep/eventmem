#!/usr/bin/env bash
# Install the eventmem Hermes adapter into a Hermes home.
#
#   ./install.sh                 # links into ~/.hermes
#   HERMES_HOME=/path ./install.sh
#
# The plugin is a SYMLINK, not a copy: edit the repository, restart the host, done.
# (Hermes loads memory providers from $HERMES_HOME/plugins/<name>/, and the directory
# name is the provider name, which is why the link is named "eventmem".)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HOME_DIR/plugins"
TARGET="$REPO/adapters/hermes/eventmem"
LINK="$PLUGIN_DIR/eventmem"

if [ ! -f "$TARGET/plugin.yaml" ]; then
  echo "install.sh: $TARGET/plugin.yaml not found — is this the repository root?" >&2
  exit 1
fi

mkdir -p "$PLUGIN_DIR"

if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  backup="$LINK.bak-$(date +%Y%m%d-%H%M%S)"
  mv "$LINK" "$backup"
  echo "moved the old plugin directory to $backup"
fi

ln -sfn "$TARGET" "$LINK"
echo "linked $LINK -> $TARGET"

cat <<EOF

Next steps
  1. config.yaml:   memory:
                      provider: 'eventmem'
                    plugins:
                      entries:
                        eventmem:
                          settings: {}     # optional; core defaults apply
  2. the model service (optional, edges only): systemd unit running
       $HOME/models/startlux/start.sh
  3. the resident worker (edges):
       systemctl --user edit eventmem-worker
       ExecStart=%h/.hermes/hermes-agent/.venv/bin/python $REPO/adapters/hermes/run.py worker --db %h/.hermes/eventmem/events.db
  4. restart the host processes that serve sessions (dashboard / gateway / desktop).
EOF
