#!/usr/bin/env bash
# P4 最终验收：CLI 全子命令 + 增量 + 自定义词典 + HTTP 端点
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
DB=/tmp/kb_final.db
rm -f "$DB"

echo "########## 1) kb build -o json（含统计字段）"
$PY -m lineage.cli kb build examples/warehouse examples/knowledge_demo --db "$DB" -o json \
  | $PY -c "import json,sys; d=json.load(sys.stdin); print('mode',d['mode']); print('counts',d['counts']); print('hash',d['knowledge_hash']); print('glossary',d['glossary']); print('files',d['file_count'])"

echo
echo "########## 2) kb build --incremental（同一份输入，指纹必须不变）"
$PY -m lineage.cli kb build examples/warehouse examples/knowledge_demo --db "$DB" --incremental --quiet | sed -n '15,17p'

echo
echo "########## 3) kb build --glossary 自定义词典叠加"
cat > /tmp/kb_custom_glossary.json <<'JSON'
{"version": "custom-test",
 "columns": {"bz": "备注（自定义词典补充）"},
 "tokens": {"zzz": "测试"}}
JSON
$PY -m lineage.cli kb build examples/warehouse examples/knowledge_demo --db /tmp/kb_final_gloss.db \
  --glossary /tmp/kb_custom_glossary.json --quiet | sed -n '6,8p;20,20p'
$PY -m lineage.cli kb terms --db /tmp/kb_final_gloss.db --source comment --limit 3

echo
echo "########## 4) kb summary -o json（关键字段）"
$PY -m lineage.cli kb summary --db "$DB" -o json | $PY -c "import json,sys; d=json.load(sys.stdin); print('counts',d['counts']); print('by_type',d['metric_by_type']); print('by_layer',d['metric_by_layer'])"

echo
echo "########## 5) kb fields（表字段清单前 12 行）"
$PY -m lineage.cli kb fields cdw.dwd_卷烟产量码段明细 --db "$DB" | sed -n '1,12p'

echo
echo "########## 6) kb export --md --json"
$PY -m lineage.cli kb export --db "$DB" --md /tmp/kb_final.md --json /tmp/kb_final.json | head -3
ls -la /tmp/kb_final.md /tmp/kb_final.json

echo
echo "########## 7) HTTP 端点（tmux lineage-api，端口 18080）"
curl -s --max-time 8 http://127.0.0.1:18080/kb/summary | $PY -c "import json,sys; d=json.load(sys.stdin); print('GET /kb/summary ->', d['success'], d['counts']['kb_metrics'], '条口径')"
curl -s --max-time 8 -X POST http://127.0.0.1:18080/kb/search -H 'Content-Type: application/json' \
  -d '{"query":"产量","kinds":["metrics"],"limit":2}' | $PY -c "import json,sys; d=json.load(sys.stdin); print('POST /kb/search ->', d['success'], d['total'], [m['formula'] for m in d['groups']['metrics']])"
curl -s --max-time 8 -X POST http://127.0.0.1:18080/kb/ask -H 'Content-Type: application/json' \
  -d '{"question":"产量怎么算的"}' | $PY -c "import json,sys; d=json.load(sys.stdin); print('POST /kb/ask ->', d['success'], d['intent'], '|', d['answer'].splitlines()[0])"
curl -s --max-time 8 "http://127.0.0.1:18080/kb/metric?name=%E4%BA%A7%E9%87%8F" | $PY -c "import json,sys; d=json.load(sys.stdin); print('GET /kb/metric ->', d['success'], d['count'], '条')"
curl -s --max-time 8 -X POST http://127.0.0.1:18080/parse -H 'Content-Type: application/json' \
  -d '{"sql":"CREATE TABLE t AS SELECT a.id AS id FROM s.a a"}' | $PY -c "import json,sys; d=json.load(sys.stdin); print('POST /parse（回归）->', d['success'], d['table_lineage'])"

echo
echo "########## 8) pytest 全量"
$PY -m pytest -q 2>&1 | tail -3
echo "pytest exit=$?"
