#!/usr/bin/env bash
# 一键验收门禁：模块化重构后所有「必须全绿」的检查，输出即证据。
# 用法：bash tools/run_gate.sh            （需要 ds-standalone 容器 + 血缘服务在跑）
#       bash tools/run_gate.sh --fast     （跳过需要容器的两项）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

# 代理变量会污染本机 127.0.0.1 请求（urllib 不认 no_proxy 里的 127.*），统一清掉
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy 2>/dev/null || true

PASS=0; FAIL=0
declare -a ROWS

step() {  # step <名称> <命令...>
  local name="$1"; shift
  local out; local rc
  out="$("$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    PASS=$((PASS+1)); ROWS+=("✅ $name")
  else
    FAIL=$((FAIL+1)); ROWS+=("❌ $name")
    echo "----- $name 失败输出（尾部 12 行）-----"
    echo "$out" | tail -12
  fi
  echo "$out" > "/tmp/gate_$(echo "$name" | tr ' /' '__').log"
}

echo "===== 验收门禁 $(date '+%F %T') ====="
echo
echo "① 单元②（后端服务/CLI）"
step "pytest 全量用例" bash -c "$PY -m pytest -v --tb=line -rN | tail -1"
step "分层守卫 check_layering" bash -c "$PY tools/check_layering.py"
step "CLI 可导入 + 子命令注册" bash -c "$PY -c \"
from lineage import cli
cmds = sorted(cli._SUBCOMMAND_RUNNERS)
assert callable(cli.main)
need = {'scan','upstream','impact','path','cycle','stats','viz','ds','kb','generate'}
assert need <= set(cmds), cmds
print('已注册子命令:', ' '.join(cmds))\""
step "CLI 真实调用（stats 子命令）" bash -c "$PY -m lineage.cli stats --graph warehouse_graph.json | head -3"

echo
echo "② 单元②↔③ 契约（HTTP）"
if [ $FAST -eq 1 ]; then
  echo "   (--fast: 跳过)"
else
  step "血缘服务健康检查 :18080" bash -c "curl -sf http://127.0.0.1:18080/health | head -c 120"
  step "单脚本血缘 /analyze（真实请求）" bash -c \
    "curl -sf -X POST http://127.0.0.1:18080/analyze -H 'Content-Type: application/json' \
     -d '{\"sql\":\"insert overwrite table ads.t1 select a.x from dws.t2 a join dwd.t3 b on a.id=b.id\",\"dialect\":\"hive\"}' \
     | $PY -c \"import json,sys; d=json.load(sys.stdin); print('表级血缘条目:', len(d.get('tables', [])), '| 字段血缘条目:', len(d.get('columns', [])))\""
fi

echo
echo "③ 单元③（海豚插件：Java + 前端补丁）"
step "前端表单真跑 verify_ldf_form" bash -c "$PY apps/ds-plugin/verify/verify_ldf_form.py | tail -2"
step "保存链路 verify_save_params" bash -c "$PY apps/ds-plugin/verify/verify_save_params.py | tail -2"
if [ $FAST -eq 0 ]; then
  step "服务端下发字节核对 verify_ui_deploy" bash -c "$PY apps/ds-plugin/verify/verify_ui_deploy.py | tail -2"
fi

echo
echo "===== 汇总：通过 $PASS 项 / 失败 $FAIL 项 ====="
for r in "${ROWS[@]}"; do echo "  $r"; done
if [ $FAIL -eq 0 ]; then
  echo; echo "GATE: ALL_PASS"
else
  echo; echo "GATE: HAS_FAILURE"
fi
exit $((FAIL > 0))
