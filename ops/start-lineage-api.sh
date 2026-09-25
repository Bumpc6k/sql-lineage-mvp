#!/usr/bin/env bash
# 拉起身血服务（血缘 + 知识库 HTTP 服务，:18080）。幂等：已在跑就不重启。
#
# 注意：必须显式设置 PYTHONPATH —— 本仓库是「多单元源码树」，不是安装包；
# 干净克隆下直接 `python -m lineage.serve.api_server` 会报 ModuleNotFoundError: lineage_core
# （协作验证报告 P1-5b）。下面按仓库根逐个挂载单元源码。
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
SESSION=${SESSION:-lineage-api}
PORT=${LINEAGE_PORT:-18080}
PY=${PY:-"$ROOT/.venv/bin/python"}
[ -x "$PY" ] || PY=python3
export PYTHONPATH="$ROOT:$ROOT/apps/lineage-api:$ROOT/packages/lineage-core"

if curl -s --noproxy '*' -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health"; then
  echo "  内核已在运行：http://127.0.0.1:$PORT/health"
  exit 0
fi

CMD="$PY -m lineage.serve.api_server --host 127.0.0.1 --port $PORT"
if command -v tmux >/dev/null 2>&1; then
  tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
  tmux new-session -d -s "$SESSION" -c "$ROOT" "$CMD"
  echo "  tmux 会话 $SESSION 已启动"
else
  # 无 tmux（如 Git Bash / 精简环境）就直接后台跑
  nohup $CMD >/tmp/lineage-api.log 2>&1 &
  echo "  无 tmux：已后台启动，日志 /tmp/lineage-api.log"
fi

for _ in $(seq 1 20); do
  curl -s --noproxy '*' -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/health" && break
  sleep 1
done
CODE=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/health" || echo 000)
echo "  内核 :$PORT → HTTP $CODE"
