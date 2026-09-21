#!/usr/bin/env bash
# 独立启动前端模块 apps/web（零构建：纯静态文件 + 本地内置 Vue 3）
#
#   bash ops/start-web.sh          # 默认 :5173
#   bash ops/start-web.sh 5199     # 指定端口
#
# 两种托管方式二选一，互不影响：
#   A. 独立部署（推荐，体现「单元①独立」）：本脚本起静态服务，浏览器开 http://localhost:5173
#   B. 蹭后端托管（演示省事）：不起本脚本，直接开 http://localhost:18080/app/
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT/apps/web"
PORT="${1:-5173}"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

if [ ! -f "$WEB_DIR/index.html" ]; then
  echo "❌ 找不到前端模块：$WEB_DIR/index.html" >&2
  exit 1
fi

if tmux has-session -t web 2>/dev/null; then
  echo "   (已有 tmux 会话 web，先关掉)"
  tmux kill-session -t web
fi

tmux new-session -d -s web -c "$WEB_DIR" \
  "$PY -m http.server $PORT --bind 0.0.0.0"
sleep 1

if curl -sf -o /dev/null "http://127.0.0.1:$PORT/index.html"; then
  echo "✅ 前端模块已启动：http://localhost:$PORT/   （tmux 会话 web，目录 $WEB_DIR）"
  echo "   后端仍需在跑：bash ops/start-lineage-api.sh   （前端顶部可改服务地址）"
else
  echo "❌ 启动失败，看日志：tmux attach -t web" >&2
  exit 1
fi
