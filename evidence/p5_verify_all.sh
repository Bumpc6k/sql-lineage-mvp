#!/usr/bin/env bash
# P5 最终全量验证：编译部署 → 恢复演示环境 → sql 模式（⑤ 业务口径）+ impact 模式回归 + 降级路径 + pytest
set -uo pipefail
PROJ=/root/projects/sql-lineage-mvp
OUT=${OUT:-/tmp/p5_evidence_final}
mkdir -p "$OUT"
cd "$PROJ"

echo "########## 1) 编译 + 部署插件（docker cp 四个 libs + 重启海豚） ##########"
bash apps/apps/ds-plugin/java/build.sh > "$OUT/build.log" 2>&1
echo "   build.sh exit=$?"
grep -E "javac 成功|产物:|-> /opt|已就绪" "$OUT/build.log"

echo
echo "########## 2) 恢复演示环境（内存库重启后清空） ##########"
bash /usr/local/bin/prep-ds-demo.sh > "$OUT/prep.log" 2>&1
echo "   prep exit=$?"
grep -E "端到端验证|最终任务实例" "$OUT/prep.log"

echo
echo "########## 3) sql 模式：血缘 + 业务口径（真实海豚任务日志） ##########"
.venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py > "$OUT/sql_log.txt" 2>&1
echo "   exit=$?"
sed -n '/6) 断言检查/,$p' "$OUT/sql_log.txt"

echo
echo "########## 4) impact 模式回归（④ 段与下游影响仍在，⑤ 不出现） ##########"
.venv/bin/python apps/apps/ds-plugin/verify/verify_impact_mode.py > "$OUT/impact_log.txt" 2>&1
echo "   exit=$?"
sed -n '/===*$/,$p' "$OUT/impact_log.txt" | tail -12

echo
echo "########## 5) 降级路径：旧版服务（无 /analyze）→ 回退 /parse ##########"
bash evidence/p5_collect_degraded_evidence.sh > "$OUT/degraded.txt" 2>&1
echo "   exit=$?"
sed -n '/6) 断言检查/,$p' "$OUT/degraded.txt"

echo
echo "########## 6) HTTP 端点回归 ##########"
bash evidence/p5_regression.sh > "$OUT/http.txt" 2>&1
echo "   exit=$?"
cat "$OUT/http.txt"

echo
echo "########## 7) pytest ##########"
env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  .venv/bin/python -m pytest -o addopts="" -q > "$OUT/pytest.txt" 2>&1
echo "   exit=$?"
tail -2 "$OUT/pytest.txt"
