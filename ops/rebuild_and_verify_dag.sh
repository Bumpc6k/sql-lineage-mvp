#!/usr/bin/env bash
# 重建插件 → 重启血缘服务 → 恢复演示环境 → 跑 LINEAGE_DAG 端到端验证
set -uo pipefail
ROOT=/root/projects/sql-lineage-mvp
cd "$ROOT" || exit 1
echo "===== 1) 编译部署插件 ====="
bash apps/apps/ds-plugin/java/build.sh 2>&1 | tail -12
echo "===== 2) 重启血缘服务（工作流级端点） ====="
bash ops/restart_lineage_api.sh 2>&1 | tail -3
echo "===== 3) 恢复演示环境（海豚重启后内存库会清空） ====="
bash ops/prep_ds_demo.sh 2>&1 | tail -12
echo "===== 4) LINEAGE_DAG 端到端验证 ====="
env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  .venv/bin/python apps/apps/ds-plugin/verify/verify_dag.py
echo "VERIFY_DAG_EXIT=$?"
