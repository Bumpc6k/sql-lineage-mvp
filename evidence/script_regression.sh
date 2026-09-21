#!/usr/bin/env bash
# =============================================================================
# 脚本解析能力回归验证（HTTP 层）：
#   1) POST /analyze            —— 单脚本 SQL 分析（原有能力不受影响）
#   2) POST /analyze-workflow   —— 工作流级分析（含 SQL + SHELL + PYTHON 三任务）
#   3) POST /generate/sql       —— 生成引擎 L1（原有能力不受影响）
#   4) POST /generate/pipeline  —— 生成引擎 L2
# 用法: bash evidence/script_regression.sh
# =============================================================================
set -uo pipefail
API=${LINEAGE_API:-http://127.0.0.1:18080}
PROJ=/root/projects/sql-lineage-mvp
wf_code=$(cd "$PROJ" && env -u http_proxy -u https_proxy .venv/bin/python - <<'PY'
import json, sys
sys.path.insert(0, ".")
from lineage.ds_client import DsClient
c = DsClient(base_url="http://localhost:12345/dolphinscheduler", user="admin", password="dolphinscheduler123")
try:
    c.ensure_login()
    for p in c.list_projects():
        if p.get("name") == "烟草数仓演示":
            for d in c.list_process_definitions(p["code"]):
                if d.get("name") == "wf_脚本解析实测":
                    print(f'{p["code"]} {d["code"]}')
finally:
    c.close()
PY
)
python_code=$(echo "$wf_code" | awk '{print $1}')
define_code=$(echo "$wf_code" | awk '{print $2}')
echo "演示工作流: project_code=$python_code process_define_code=$define_code"
echo

echo "=== 1) POST /analyze（单脚本 SQL）==="
curl -s -m 60 -X POST "$API/analyze" -H 'Content-Type: application/json' -d '{
  "sql": "INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '"'"'2026-01-01'"'"')\nSELECT p.work_order_no AS work_order_no, p.plant_code AS plant_code, p.output_qty AS output_qty\nFROM ods.ods_卷烟产量流水 p LEFT JOIN dim.dim_plant pl ON p.plant_code = pl.plant_code WHERE p.dt = '"'"'2026-01-01'"'"'",
  "task_name": "回归-单脚本", "with_report": false
}' | head -c 700
echo; echo

echo "=== 2) POST /analyze-workflow（SQL + SHELL + PYTHON）==="
curl -s -m 120 -X POST "$API/analyze-workflow" -H 'Content-Type: application/json' -d "{
  \"project_code\": $python_code, \"process_define_code\": $define_code,
  \"task_types\": [\"SQL\", \"SHELL\", \"PYTHON\"], \"with_report\": false
}" > /tmp/analyze_wf.json
env -u http_proxy -u https_proxy "$PROJ/.venv/bin/python" - <<'PY'
import json
r = json.load(open("/tmp/analyze_wf.json", encoding="utf-8"))
if not r.get("success"):
    print("FAILED:", r.get("error")); raise SystemExit(1)
w = r["workflow"]
print("success=%s 工作流=%s 任务=%s 解析成功=%s" % (r["success"], w["name"], w["task_count"], w["parsed_task_count"]))
print("语句=%s SQL面=%s 脚本类型分布=%s 未解析提示=%s" % (
    w["statement_count"], w.get("sql_count"), w.get("script_kinds"), w.get("unresolved_hint_count")))
for t in r["tasks"]:
    print("  - %-16s type=%-7s script_kind=%-7s sql_count=%s 语句=%s 输出表=%s" % (
        t["name"], t["type"], t["script_kind"], t["sql_count"], t["statement_count"], t["output_tables"]))
print("表级血缘: %s 条 / 字段级: %s 条" % (len(r["merged"]["table_lineage"]), r["merged"]["column_lineage_count"]))
PY
echo

echo "=== 3) POST /generate/sql（生成引擎 L1）==="
curl -s -m 60 -X POST "$API/generate/sql" -H 'Content-Type: application/json' -d '{
  "source_tables": ["ods.ods_卷烟产量流水"], "target_table": "cdw.dwd_卷烟产量回归",
  "metrics": ["产量"], "group_by": ["plant_code"], "partition": "dt"
}' | head -c 400
echo; echo

echo "=== 4) POST /generate/pipeline（生成引擎 L2）==="
curl -s -m 60 -X POST "$API/generate/pipeline" -H 'Content-Type: application/json' -d '{
  "requirement": "生成产销存月报", "target_layer": "ads", "max_stages": 3
}' | head -c 400
echo
