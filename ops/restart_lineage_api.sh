#!/usr/bin/env bash
# 重启血缘服务（强制）：杀掉旧进程 → 重新拉起 → 等 /health
set -uo pipefail
pkill -f "lineage.api_server" || true
sleep 2
pgrep -f "lineage.api_server" && { echo "还有残留进程"; pkill -9 -f "lineage.api_server"; sleep 1; }
bash /usr/local/bin/start-lineage-api.sh
echo "--- health ---"
curl -s -m 5 http://127.0.0.1:18080/health
echo
