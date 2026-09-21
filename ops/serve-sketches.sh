#!/usr/bin/env bash
# 起一个静态服务把 sketches/ 下的界面原型端出来看（原型专用，非生产）
#   bash ops/serve-sketches.sh [端口]   -> http://localhost:5180/
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-5180}"
tmux kill-session -t sketches 2>/dev/null || true
tmux new-session -d -s sketches -c "$ROOT/sketches" "python3 -m http.server $PORT --bind 0.0.0.0"
sleep 1
for d in $(ls "$ROOT/sketches" | grep -E '^[0-9]'); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/$d/index.html")
  echo "  $code  http://localhost:$PORT/$d/"
done
