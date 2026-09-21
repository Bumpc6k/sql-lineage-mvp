#!/usr/bin/env bash
# P5 证据采集：#3 降级路径 —— 血缘服务「旧版本」（无 /analyze）时插件回退 /parse
set -uo pipefail
PROJ=/root/projects/sql-lineage-mvp
OUT=/tmp/p5_evidence
mkdir -p "$OUT"
PORT=18099

echo "== 1) 起一个旧版代理（/analyze 返回 404，其余转发真实服务）=="
tmux kill-session -t p5-legacy 2>/dev/null || true
tmux new-session -d -s p5-legacy "cd $PROJ && .venv/bin/python evidence/p5_legacy_proxy.py --port $PORT >> $OUT/legacy_proxy.log 2>&1"
sleep 3
echo -n "   /analyze 探测: "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://127.0.0.1:$PORT/analyze" -H 'Content-Type: application/json' -d '{"sql":"select 1"}'
echo -n "   /parse 探测  : "
curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://127.0.0.1:$PORT/parse" -H 'Content-Type: application/json' -d '{"sql":"CREATE TABLE t AS SELECT a.id AS id FROM s.a a","dialect":"hive"}'

echo
echo "== 2) 用「旧版服务地址」跑一次 LINEAGE 任务，验证降级 =="
cd "$PROJ"
LINEAGE_SERVICE_URL="http://172.17.0.1:$PORT" LINEAGE_WF_NAME="wf_lineage_降级演示" \
  .venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py --degraded > "$OUT/degraded_log.txt" 2>&1
echo "   exit=$?"
sed -n '/6) 断言检查/,$p' "$OUT/degraded_log.txt"

echo
echo "== 3) 关掉旧版代理 =="
tmux kill-session -t p5-legacy 2>/dev/null || true
echo "   已关闭 p5-legacy"
