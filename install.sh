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
    # Never leave the backup inside a scan root: the host loads every subdirectory, so a
    # stale copy with the same plugin id is a "duplicate id" error at startup (seen on
    # 2026-11-12 with eventmem-graph). Backups live next to the roots, not in them.
    local backup="$HOME_DIR/plugin-backups/$(basename "$(dirname "$link")")-$(basename "$link").bak-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$(dirname "$backup")"
    mv "$link" "$backup"
    echo "moved the old directory to $backup (outside the plugin roots)"
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
# The daemon's GUI, proxied through the dashboard so it also works when the desktop app is
# connected to a remote backend over SSH (a browser there cannot open the remote loopback).
link "$REPO/adapters/hermes/eventmem-gui" \
     "$HOME_DIR/plugins/eventmem-gui"
link "$REPO/adapters/hermes/desktop-plugins/eventmem-gui" \
     "$HOME_DIR/desktop-plugins/eventmem-gui"

cat <<EOF

Next steps
  1. config.yaml:   memory:
                      provider: 'eventmem'
                    plugins:
                      enabled: [eventmem-graph, eventmem-gui]   # both are required
                      entries:
                        eventmem:
                          settings: {}     # optional; core defaults apply

     `enabled` is not decoration: a user plugin's Python `api` file is only imported when it is
     in `plugins.enabled` (code-execution gate). Forgetting it mounts nothing and the GUI page
     comes back empty.
  2. the model service (optional, edges only): systemd unit running
       $HOME/models/startlux/start.sh
  3. the daemon (edges + the GUI), one per database:
       systemctl --user edit eventmem-worker
       ExecStart=%h/.hermes/hermes-agent/.venv/bin/python $REPO/adapters/hermes/run.py daemon --db %h/.hermes/eventmem/events.db
  4. restart the host processes that serve sessions (dashboard / gateway / desktop).
     desktop plugins: Cmd/Ctrl-K -> Reload desktop plugins
  5. the GUI: status bar chip "记忆后台", or http://127.0.0.1:9123/ directly, or
     <dashboard>/api/plugins/eventmem-gui/ when the backend is remote
     (EVENTMEM_DAEMON_URL moves the proxy's target)
EOF
