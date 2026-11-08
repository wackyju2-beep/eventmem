#!/usr/bin/env bash
# Link eventmem into a Hermes home. Safe to re-run: existing targets are left alone.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}

mkdir -p "$HERMES_HOME/plugins" "$HERMES_HOME/desktop-plugins"

link() { # src dst
  if [ -e "$2" ] || [ -L "$2" ]; then
    echo "skip  $2  (已存在，没动它)"
  else
    ln -s "$1" "$2"
    echo "link  $2 -> $1"
  fi
}

link "$HERE/plugins/eventmem" "$HERMES_HOME/plugins/eventmem"
link "$HERE/plugins/eventmem-graph" "$HERMES_HOME/plugins/eventmem-graph"
link "$HERE/desktop-plugins/eventmem-graph" "$HERMES_HOME/desktop-plugins/eventmem-graph"

cat <<'EOF'

下一步：
  hermes config set memory.provider eventmem      # 启用记忆
  hermes plugins enable eventmem-graph            # 要 3D 图谱才需要
  # 重启会话 / gateway 后生效

可选：关系边（没有也能用，只是没有边）
  python3 plugins/eventmem/worker.py --once       # 手动跑一轮
  python3 plugins/eventmem/worker.py              # 常驻，每 60s 看一次
EOF
