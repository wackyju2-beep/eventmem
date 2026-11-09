#!/usr/bin/env bash
# Install the eventmem Hermes adapter into a Hermes home.
#
#   ./install.sh                 # links into ~/.hermes
#   HERMES_HOME=/path ./install.sh
#
# The three directories are SYMLINKS, not copies: edit the repository, restart the host, done.
# Hermes loads memory providers and dashboard plugins from $HERMES_HOME/plugins/<name>/, and
# desktop plugins from $HERMES_HOME/desktop-plugins/<name>/. The link names are the plugin
# names, which is why they are "eventmem" and "eventmem-graph".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

link() {
  local target="$1" link="$2"
  if [ ! -e "$target" ]; then
    echo "install.sh: $target not found — is this the repository root?" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$link")"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    local backup="$link.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$link" "$backup"
    echo "moved the old directory to $backup"
  fi
  ln -sfn "$target" "$link"
  echo "linked $link -> $target"
}

link "$REPO/adapters/hermes/eventmem" \
     "$HOME_DIR/plugins/eventmem"
link "$REPO/adapters/hermes/eventmem-graph" \
     "$HOME_DIR/plugins/eventmem-graph"
link "$REPO/adapters/hermes/desktop-plugins/eventmem-graph" \
     "$HOME_DIR/desktop-plugins/eventmem-graph"

cat <<EOF

Next steps
  1. config.yaml:   memory:
                      provider: 'eventmem'
                    plugins:
                      enabled: [eventmem-graph]   # optional, the graph only
                      entries:
                        eventmem:
                          settings: {}     # optional; core defaults apply
  2. the model service (optional, edges only): systemd unit running
       $HOME/models/startlux/start.sh
  3. the resident worker (edges):
       systemctl --user edit eventmem-worker
       ExecStart=%h/.hermes/hermes-agent/.venv/bin/python $REPO/adapters/hermes/run.py worker --db %h/.hermes/eventmem/events.db
  4. restart the host processes that serve sessions (dashboard / gateway / desktop).
     desktop plugins: Cmd/Ctrl-K -> Reload desktop plugins
EOF
