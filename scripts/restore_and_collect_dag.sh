#!/usr/bin/env bash
# 把演示环境恢复成「干净 + 完整」的状态，并重新采集 LINEAGE_DAG 证据
set -uo pipefail
ROOT=/root/projects/sql-lineage-mvp
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
export -n http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null || true

echo "===== 0) 把「烟草数仓演示」下所有定义先下线（海豚删除要求 OFFLINE） ====="
"$PY" - <<'PY'
from lineage.ds_client import DsClient
c = DsClient(); c.ensure_login()
for p in c.list_projects():
    if p["name"] != "烟草数仓演示":
        continue
    for d in c.list_process_definitions(p["code"]):
        c.request("POST", f"/projects/{p['code']}/process-definition/{d['code']}/release",
                  params={"releaseState": "OFFLINE"}, raise_on_error=False)
        print(f"  offline {d['name']} ({d['code']})")
PY

echo "===== 1) 重建演示数据（4 个工作流 / 16 个任务） ====="
"$PY" scripts/ds_setup_demo.py --no-export 2>&1 | tail -12

echo "===== 2) LINEAGE_DAG 端到端（建 2 个演示工作流 + 给 wf_dws_汇总 追加节点） ====="
"$PY" ds-plugin/verify_dag.py > /tmp/verify_dag.log 2>&1
echo "verify_dag exit=$?"
grep -E "回读工作流任务|实例 [0-9]|SUCCESS to master|已把工作流置回 OFFLINE" /tmp/verify_dag.log | tail -12

echo "===== 3) 重新采集 /analyze-workflow 证据 ====="
bash scripts/collect_dag_evidence.sh > docs/p6_evidence/01_analyze_workflow_and_frontend.txt 2>&1
echo "collect exit=$?"
head -6 docs/p6_evidence/01_analyze_workflow_and_frontend.txt
cp /tmp/dag_evidence/analyze_workflow.json docs/p6_evidence/03_analyze_workflow_response.json
cp /tmp/dag_evidence/report.html docs/p6_evidence/04_workflow_lineage_report.html
cp /tmp/verify_dag.log docs/p6_evidence/02_ds_task_log_lineage_dag.txt

echo "===== 4) 全量单测 ====="
"$PY" -m pytest -o addopts="" -q 2>&1 | tail -3 | tee docs/p6_evidence/06_pytest.txt
echo "DONE"
