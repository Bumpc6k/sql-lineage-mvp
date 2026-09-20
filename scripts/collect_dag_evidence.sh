#!/usr/bin/env bash
# 采集 /analyze-workflow 的真实 HTTP 证据（curl + 报告页 + 前端文件 200）
#
# 工作流（项目/工作流 code）是**动态解析**的：海豚重启会清内存库、重建演示数据后 code 会变，
# 写死 code 会让证据失效。这里用 DsClient 先查出「烟草数仓演示 / wf_dwd_清洗」的当前 code。
set -uo pipefail
ROOT=/root/projects/sql-lineage-mvp
OUT=/tmp/dag_evidence
mkdir -p "$OUT"

read -r PC WF <<<"$("$ROOT/.venv/bin/python" - <<'PY'
from lineage.ds_client import DsClient
c = DsClient(); c.ensure_login()
for p in c.list_projects():
    if p["name"] == "烟草数仓演示":
        for d in c.list_process_definitions(p["code"]):
            if d["name"] == "wf_dwd_清洗":
                print(p["code"], d["code"]); raise SystemExit
raise SystemExit("找不到 烟草数仓演示 / wf_dwd_清洗（先跑 scripts/ds_setup_demo.py）")
PY
)"
echo "解析到工作流：项目 code=$PC  工作流 code=$WF（烟草数仓演示 / wf_dwd_清洗）"

echo "===== 1) POST /analyze-workflow（真实 curl，打的是烟草数仓演示/wf_dwd_清洗）====="
curl -s --max-time 60 -X POST http://localhost:18080/analyze-workflow \
  -H 'Content-Type: application/json' \
  -d "{\"project_code\": $PC, \"process_define_code\": $WF, \"scope\": \"current\", \
       \"task_types\": [\"SQL\",\"SHELL\",\"PYTHON\"], \"include_sub_process\": false, \
       \"with_knowledge\": true, \"with_report\": true}" \
  -o "$OUT/analyze_workflow.json" -w 'HTTP %{http_code}  bytes=%{size_download}  time=%{time_total}s\n'

python3 - <<PY
import json
d = json.load(open("$OUT/analyze_workflow.json", encoding="utf-8"))
print("success:", d["success"])
print("workflow:", json.dumps({k: v for k, v in d["workflow"].items() if k != "workflows"}, ensure_ascii=False))
print("workflows:", json.dumps(d["workflow"]["workflows"], ensure_ascii=False))
print("tasks:")
for t in d["tasks"]:
    print("   ", json.dumps(t, ensure_ascii=False))
print("chain:", " → ".join(d["chain"]))
print("chain_in_workflow:", d["chain_in_workflow"], "| chain_external:", json.dumps(d["chain_external"], ensure_ascii=False))
print("merged.n(table_lineage)=", len(d["merged"]["table_lineage"]),
      " nodes=", len(d["merged"]["nodes"]), " edges=", len(d["merged"]["edges"]),
      " column_lineage=", d["merged"]["column_lineage_count"])
print("merged.table_lineage[0:3]:", json.dumps(d["merged"]["table_lineage"][:3], ensure_ascii=False))
print("merged.task_lineage:", json.dumps(d["merged"]["task_lineage"], ensure_ascii=False))
print("merged.column_lineage[0]:", json.dumps(d["merged"]["column_lineage"][0], ensure_ascii=False))
print("knowledge: kb_available=", d["knowledge"]["kb_available"], " metric_count=", d["knowledge"]["metric_count"],
      " terms=", len(d["knowledge"].get("terms") or []), " rules=", len(d["knowledge"].get("rules") or []))
print("quality.summary:", json.dumps(d["quality"]["summary"], ensure_ascii=False))
print("quality.cycles:", json.dumps(d["quality"]["cycles"], ensure_ascii=False),
      "missing_knowledge:", json.dumps(d["quality"]["missing_knowledge"], ensure_ascii=False))
print("quality.dangling_outputs[0]:", json.dumps(d["quality"]["dangling_outputs"][0], ensure_ascii=False))
print("report_id:", d["report_id"], "| url:", d["url"], "| internal:", d["internal_url"])
print("cost_ms:", d["cost_ms"], "| ds_base:", d["ds_base"], "| dialect:", d["dialect"])
open("$OUT/report_url.txt", "w").write(d["url"])
PY

echo
echo "===== 2) GET /report/<id>（工作流级 HTML 报告）====="
RU=$(cat "$OUT/report_url.txt")
curl -s --max-time 30 -o "$OUT/report.html" -w "HTTP %{http_code}  content_type=%{content_type}  bytes=%{size_download}\n" "$RU"
echo "--- HTML 前 15 行 ---"
head -15 "$OUT/report.html"
echo "--- 工作流级关键区块是否都在 ---"
for k in 工作流概览 任务清单 全链路（跨任务） 链路质量体检 来源任务 全链路图 WORKFLOW; do
  printf '  %-22s %s\n' "$k" "$(grep -c "$k" "$OUT/report.html")"
done

echo
echo "===== 3) 前端 4 个文件：HTTP 200 + 含 LINEAGE_DAG ====="
for f in dag-sidebar.340ad7aa.js task-type.27c43290.js detail-modal.de80dd20.js detail.f1164795.js; do
  code=$(curl -s -o "$OUT/$f" -w '%{http_code}' --max-time 10 "http://localhost:12345/dolphinscheduler/ui/assets/$f")
  n=$(grep -o 'LINEAGE_DAG' "$OUT/$f" | wc -l)
  echo "  $f  HTTP=$code  LINEAGE_DAG出现=$n 次"
done
echo "--- sidebar 里新增的那一项 ---"
grep -o 't.dataList.push({taskType:"LINEAGE_DAG"[^)]*)' "$OUT/dag-sidebar.340ad7aa.js"
echo "--- detail.js 表单映射 ---"
grep -o ',LINEAGE_DAG:Rr}' "$OUT/detail.f1164795.js"
echo "--- task-type / detail-modal 类型表 ---"
grep -o 'LINEAGE_DAG:{alias:"LINEAGE_DAG",helperLinkDisable:!0}' "$OUT/task-type.27c43290.js" "$OUT/detail-modal.de80dd20.js"

echo
echo "===== 4) 任务类型配置与动态表单（容器内） ====="
docker exec ds-standalone sh -c 'grep -n "LINEAGE" /opt/dolphinscheduler/conf/dynamic-task-type-config.yaml'
docker exec ds-standalone sh -c 'ls -la /opt/dolphinscheduler/ui/static/lineage/'
