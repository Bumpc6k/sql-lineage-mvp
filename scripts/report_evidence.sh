#!/usr/bin/env bash
# ============================================================================
# HTML 血缘报告端点证据采集：POST /report → GET /report/<id>
#
# 为什么用 Python 造请求体：演示 SQL 里有中文、单引号、换行、注释，
# 手写 JSON 极易转义出错 —— 交给 json.dumps 负责转义，curl 只管发。
#
# 用法:
#   bash scripts/report_evidence.sh [SQL 文件] [服务地址]
# 默认:
#   SQL = examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql
#   服务 = http://127.0.0.1:18080
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="${1:-$ROOT/examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql}"
BASE="${2:-http://127.0.0.1:18080}"
PY="$ROOT/.venv/bin/python"
CURL=(curl -s -m 60 --noproxy '*')
BODY="/tmp/lineage_report_body.json"
HEAD="/tmp/lineage_report.json"

step() { printf '\n\033[1;36m━━━━ %s\033[0m\n' "$*"; }

step "1) 用 Python 造请求体（转义中文/引号/换行）"
"$PY" - "$SQL_FILE" "$BODY" <<'PY'
import json, sys, pathlib
sql = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    "sql": sql, "dialect": "hive", "with_knowledge": True,
    "task_name": "t_lineage_产量口径（报告端点验证）",
}, ensure_ascii=False), encoding="utf-8")
print(f"   SQL 文件: {sys.argv[1]}  ({len(sql)} 字符) -> {sys.argv[2]}")
PY

step "2) POST $BASE/report （请求 body 与 /analyze 一致）"
"${CURL[@]}" -X POST "$BASE/report" -H 'Content-Type: application/json' \
      --data-binary "@$BODY" -o "$HEAD" -w '   HTTP 状态: %{http_code}   耗时: %{time_total}s   响应字节: %{size_download}\n'
"$PY" -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(d, ensure_ascii=False, indent=2))
' "$HEAD"

REPORT_ID="$("$PY" -c '
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("report_id", ""))
' "$HEAD")"

if [ -z "$REPORT_ID" ]; then
  echo "❌ 没拿到 report_id，后续跳过"; exit 1
fi

step "3) GET $BASE/report/$REPORT_ID"
"${CURL[@]}" -D /tmp/lineage_report_headers.txt -o "/tmp/$REPORT_ID.html" \
      -w '   HTTP 状态: %{http_code}   响应字节: %{size_download}   耗时: %{time_total}s\n' \
      "$BASE/report/$REPORT_ID"
echo "   --- 响应头（截取） ---"
grep -iE '^(HTTP/|content-type|content-length|cache-control)' /tmp/lineage_report_headers.txt | sed 's/^/   /'

step "4) HTML 前 20 行"
head -20 "/tmp/$REPORT_ID.html" | sed 's/^/   /'

step "5) 报告自检"
"$PY" - "/tmp/$REPORT_ID.html" <<'PY'
import re, sys, pathlib
html = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
checks = [
    ("单文件（无 <link>）", "<link" not in html),
    ("无 <img> 外链", "<img" not in html),
    ("无外链 script/link/src=http", not re.search(r'(src|href)="https?://', html)),
    ("内联 <script>（字段过滤）", "<script>" in html and "src=" not in html.split("<script>")[1][:60]),
    ("表级血缘段", "表级血缘" in html),
    ("字段级真表格", '<table id="col-table">' in html and html.count("<tr data-key=") > 0),
    ("口径卡片", html.count('<div class="card">') > 0),
    ("上游链路内联 SVG", "<svg" in html and "marker-end=\"url(#arrow)\"" in html),
    ("页脚署名", "由 sql-lineage-mvp 生成" in html),
    ("中文正常（UTF-8 元信息）", 'charset="utf-8"' in html),
]
for name, ok in checks:
    print(f"   {'✅' if ok else '❌'} {name}")
print(f"   报告文件大小: {len(html.encode('utf-8'))} 字节 / {len(html.splitlines())} 行")
cards = html.count('<div class="card">')
print(f"   字段映射行数: {html.count(chr(60) + 'tr data-key=')}   口径卡片数: {cards}")
PY
