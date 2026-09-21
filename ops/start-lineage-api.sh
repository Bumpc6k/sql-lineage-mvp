#!/usr/bin/env bash
# ============================================================
# 血缘解析 HTTP 服务启动脚本（供 DolphinScheduler 插件调用）
# 幂等：已在运行则跳过。日志: /var/log/lineage-api.log
# 用法: bash ops/start-lineage-api.sh
# ============================================================
set -uo pipefail
PORT=${LINEAGE_API_PORT:-18080}
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
APP="$ROOT/apps/lineage-api"          # 后端服务单元（lineage 包所在目录）
PY="$ROOT/.venv/bin/python"
LOG=/var/log/lineage-api.log
SESSION=lineage-api

if pgrep -f "lineage.api_server" > /dev/null 2>&1; then
    echo "血缘服务已在运行（端口 $PORT）"
    exit 0
fi
[ -x "$PY" ] || { echo "❌ 找不到 venv python: $PY"; exit 1; }
cd "$APP" || exit 1
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" "$PY -m lineage.api_server --host 0.0.0.0 --port $PORT >> $LOG 2>&1"
sleep 3
if pgrep -f "lineage.api_server" > /dev/null 2>&1; then
    echo "✅ 血缘服务已启动: http://0.0.0.0:$PORT （tmux 会话 $SESSION，工作目录 $APP）"
    curl -s -m 5 "http://localhost:$PORT/health" | head -c 200
    echo
else
    echo "❌ 启动失败，请查看 $LOG"; tail -10 "$LOG" 2>/dev/null; exit 1
fi
