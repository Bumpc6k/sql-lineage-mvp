#!/usr/bin/env bash
# 一次性收集 P4 交付证据（真实输出），写入 docs/p4_evidence/ 便于贴进 README
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=docs/p4_evidence
mkdir -p "$OUT"
PY=.venv/bin/python

echo "== 1) kb build（warehouse + knowledge_demo）=="
$PY -m lineage.cli kb build > "$OUT/01_kb_build.txt" 2>&1
echo "== 2) kb build（只扫 examples/warehouse）=="
$PY -m lineage.cli kb build examples/warehouse --db /tmp/kb_warehouse_only.db > "$OUT/02_kb_build_warehouse_only.txt" 2>&1
echo "== 3) kb summary =="
$PY -m lineage.cli kb summary > "$OUT/03_kb_summary.txt" 2>&1
echo "== 4) kb search 产量 =="
$PY -m lineage.cli kb search 产量 > "$OUT/04_kb_search_chanliang.txt" 2>&1
echo "== 5) kb show chanliang_qty =="
$PY -m lineage.cli kb show chanliang_qty > "$OUT/05_kb_show_chanliang_qty.txt" 2>&1
echo "== 6) kb show defect_rate =="
$PY -m lineage.cli kb show defect_rate > "$OUT/06_kb_show_defect_rate.txt" 2>&1
echo "== 7) kb ask 产量怎么算的 =="
$PY -m lineage.cli kb ask "产量怎么算的" > "$OUT/07_kb_ask_chanliang.txt" 2>&1
echo "== 8) kb ask 哪些表用到了打码量 =="
$PY -m lineage.cli kb ask "哪些表用到了打码量" > "$OUT/08_kb_ask_usage.txt" 2>&1
echo "== 9) kb ask 卷烟产量流水从哪来 =="
$PY -m lineage.cli kb ask "卷烟产量流水从哪来" > "$OUT/09_kb_ask_upstream.txt" 2>&1
echo "== 10) kb export --md --json =="
$PY -m lineage.cli kb export --md --json data/knowledge_export.json > "$OUT/10_kb_export.txt" 2>&1
head -30 docs/业务口径知识库.md > "$OUT/11_md_head30.txt"
wc -l docs/业务口径知识库.md >> "$OUT/11_md_head30.txt"
echo "== 12) kb terms --pending / kb fields / kb terms =="
$PY -m lineage.cli kb terms --pending > "$OUT/12_kb_terms_pending.txt" 2>&1
$PY -m lineage.cli kb terms --limit 12 > "$OUT/13_kb_terms.txt" 2>&1
$PY -m lineage.cli kb fields cdw.dwd_卷烟产量码段明细 > "$OUT/14_kb_fields.txt" 2>&1
echo "== 15) 幂等 / 增量 =="
$PY - > "$OUT/15_idempotent.txt" 2>&1 <<'PY'
import os
from lineage.knowledge import build_knowledge_base
db = "/tmp/kb_idem.db"
if os.path.exists(db):
    os.remove(db)
dirs = ["examples/warehouse", "examples/knowledge_demo"]
a = build_knowledge_base(dirs, db_path=db)
b = build_knowledge_base(dirs, db_path=db)
c = build_knowledge_base(dirs, db_path=db, mode="incremental")
print("rebuild #1 :", a.knowledge_hash, a.counts)
print("rebuild #2 :", b.knowledge_hash)
print("incremental:", c.knowledge_hash)
print("三者内容指纹一致 =", a.knowledge_hash == b.knowledge_hash == c.knowledge_hash)
PY
echo "== 16) HTTP 端点 =="
{
  echo '$ curl -s http://127.0.0.1:18080/kb/summary'
  curl -s --max-time 8 http://127.0.0.1:18080/kb/summary
  echo; echo
  echo '$ curl -s -X POST http://127.0.0.1:18080/kb/search -H "Content-Type: application/json" -d "{\"query\":\"产量\",\"kinds\":[\"metrics\"],\"limit\":3}"'
  curl -s --max-time 8 -X POST http://127.0.0.1:18080/kb/search \
       -H 'Content-Type: application/json' \
       -d '{"query":"产量","kinds":["metrics"],"limit":3}'
  echo; echo
  echo '$ curl -s -X POST http://127.0.0.1:18080/kb/ask -H "Content-Type: application/json" -d "{\"question\":\"产量怎么算的\"}"'
  curl -s --max-time 8 -X POST http://127.0.0.1:18080/kb/ask \
       -H 'Content-Type: application/json' -d '{"question":"产量怎么算的"}'
  echo; echo
  echo '$ curl -s "http://127.0.0.1:18080/kb/metric?name=%E4%BA%A7%E9%87%8F"'
  curl -s --max-time 8 "http://127.0.0.1:18080/kb/metric?name=%E4%BA%A7%E9%87%8F"
  echo; echo
  echo '$ curl -s http://127.0.0.1:18080/health'
  curl -s --max-time 8 http://127.0.0.1:18080/health
} > "$OUT/16_http.txt" 2>&1
echo "== 17) pytest 全量 =="
$PY -m pytest -q > "$OUT/17_pytest.txt" 2>&1
$PY -m pytest --collect-only -q > "$OUT/18_pytest_collect.txt" 2>&1
echo "OK -> $OUT"
ls -la "$OUT"
