#!/usr/bin/env bash
# P5 回归：所有 HTTP 端点（旧 + 新）都打一遍真实请求
set -uo pipefail
PY=/root/projects/sql-lineage-mvp/.venv/bin/python
BASE=http://127.0.0.1:18080

echo "=== GET /health ==="
curl -s -m 8 "$BASE/health" | $PY -c "import json,sys; d=json.load(sys.stdin); print('endpoints:', d['endpoints']); print('kb_metrics:', d['kb_metrics'])"

echo
echo "=== POST /parse （旧端点）==="
curl -s -m 15 -X POST "$BASE/parse" -H 'Content-Type: application/json' \
  -d '{"sql":"INSERT INTO dwd.t_order SELECT id, amt, dt FROM ods.t_order_src WHERE dt = '"'"'2026-01-01'"'"'","dialect":"hive"}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'in=',d['input_tables'],'out=',d['output_tables'],'cols=',d['column_lineage_count'],'has_knowledge=', 'knowledge' in d)"

echo
echo "=== POST /analyze （新端点）==="
curl -s -m 15 -X POST "$BASE/analyze" -H 'Content-Type: application/json' \
  -d '{"sql":"INSERT INTO dwd.t_order SELECT id, amt, dt FROM ods.t_order_src WHERE dt = '"'"'2026-01-01'"'"'","dialect":"hive"}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); k=d['knowledge']; print('success=',d['success'],'kb_available=',k['kb_available'],'metrics=',k['metric_count'],'reason=',k.get('reason','-'))"

echo
echo "=== POST /impact ==="
curl -s -m 15 -X POST "$BASE/impact" -H 'Content-Type: application/json' \
  -d '{"table":"ods.ods_卷烟码段流水","graph":"warehouse_graph.json","depth":3}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'found=',d['found'],'downstream_count=',d['downstream_count'])"

echo
echo "=== POST /upstream ==="
curl -s -m 15 -X POST "$BASE/upstream" -H 'Content-Type: application/json' \
  -d '{"table":"cdw.dwd_卷烟产量码段明细","graph":"warehouse_graph.json"}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'found=',d['found'],'upstream_count=',d['upstream_count'],'paths=',d['paths'][:2])"

echo
echo "=== GET /kb/summary ==="
curl -s -m 15 "$BASE/kb/summary" | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'counts=',d['counts'])"

echo
echo "=== POST /kb/search ==="
curl -s -m 15 -X POST "$BASE/kb/search" -H 'Content-Type: application/json' \
  -d '{"query":"产量","kinds":["metrics"],"limit":3}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'total=',d['total'],'top=',d['groups']['metrics'][0]['formula'])"

echo
echo "=== POST /kb/ask ==="
curl -s -m 20 -X POST "$BASE/kb/ask" -H 'Content-Type: application/json' \
  -d '{"question":"产量怎么算的","use_llm":"off"}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'intent=',d['intent'],'|',d['answer'].splitlines()[0])"

echo
echo "=== GET /kb/metric ==="
curl -s -m 15 "$BASE/kb/metric?name=chanliang_qty" | $PY -c "import json,sys; d=json.load(sys.stdin); print('success=',d['success'],'count=',d['count'],'formula=',d['metrics'][0]['formula'])"

echo
echo "=== /analyze 边界：空 SQL / 不存在的库 / with_knowledge=false ==="
curl -s -m 10 -X POST "$BASE/analyze" -H 'Content-Type: application/json' -d '{"sql":"  "}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('空 SQL -> success=',d['success'],d.get('error'))"
curl -s -m 10 -X POST "$BASE/analyze" -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT 1","db":"/tmp/no_such_kb.db"}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); k=d['knowledge']; print('库不存在 -> success=',d['success'],'kb_available=',k['kb_available'],'hint=',k['hint'])"
curl -s -m 10 -X POST "$BASE/analyze" -H 'Content-Type: application/json' \
  -d '{"sql":"INSERT INTO dwd.t_order SELECT id FROM ods.t_order_src","with_knowledge":false}' \
  | $PY -c "import json,sys; d=json.load(sys.stdin); k=d['knowledge']; print('with_knowledge=false -> success=',d['success'],'kb_available=',k['kb_available'],'reason=',k.get('reason'))"
