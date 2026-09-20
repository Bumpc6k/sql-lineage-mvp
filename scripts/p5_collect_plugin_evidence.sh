#!/usr/bin/env bash
# P5 证据采集：#2 真实编译部署插件 + 在海豚里跑出带业务口径的任务日志
# 输出：/tmp/p5_evidence/prep.log、/tmp/p5_evidence/plugin_log.txt
set -uo pipefail
PROJ=/root/projects/sql-lineage-mvp
OUT=/tmp/p5_evidence
mkdir -p "$OUT"

echo "== 1) 恢复海豚演示环境（重启后内存库清空，插件 jar 已在上一步 docker cp 进场）=="
bash /usr/local/bin/prep-ds-demo.sh > "$OUT/prep.log" 2>&1
echo "   prep-ds-demo.sh exit=$? （日志 $OUT/prep.log）"
tail -3 "$OUT/prep.log"

echo
echo "== 2) 用「产量口径」SQL 建 LINEAGE 工作流跑一次，拉真实任务日志 =="
cd "$PROJ"
.venv/bin/python ds-plugin/verify_knowledge.py > "$OUT/plugin_log.txt" 2>&1
echo "   verify_knowledge.py exit=$?"
tail -15 "$OUT/plugin_log.txt"
