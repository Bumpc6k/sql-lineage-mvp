#!/usr/bin/env bash
# ============================================================================
# 演示环境一键准备（DolphinScheduler 血缘分析插件 + 演示数据）
#
# 为什么需要它：
#   ds-standalone 用的是**内存 H2 数据库**，海豚一重启，项目/工作流/实例全部丢失；
#   插件 jar 与 UI 配置也只在容器内（无挂载卷），容器重建即消失。
#   本脚本把「重启后恢复」和「容器没了重建」两件事都做成一条命令。
#
# 用法:
#   bash scripts/prep_ds_demo.sh                # 常规：恢复插件 + 重建演示数据 + 端到端验证
#   bash scripts/prep_ds_demo.sh --skip-verify  # 只恢复环境，不跑验证工作流
#   bash scripts/prep_ds_demo.sh --skip-data    # 只恢复插件/配置，不动演示数据
#   bash scripts/prep_ds_demo.sh --force-restart# 无条件重启海豚
#
# 完成后：
#   海豚 UI  http://localhost:12345/dolphinscheduler/ui/   admin / dolphinscheduler123
#   血缘服务 http://localhost:18080/health
# ============================================================================
set -uo pipefail

# 定位项目根目录：先解析软链接（脚本常被 ln -s 到 /usr/local/bin 调用），
# 否则 dirname 会得到 /usr/local 这种错误路径。
SCRIPT_REAL="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SCRIPT_REAL")/.." && pwd)"
if [ ! -d "$ROOT/ds-plugin" ]; then
  echo "❌ 无法定位项目目录（推导为 $ROOT，其中没有 ds-plugin/）" >&2
  echo "   请用项目内路径运行：bash <项目>/scripts/prep_ds_demo.sh" >&2
  exit 1
fi
PLUGIN_DIR="$ROOT/ds-plugin"
CONTAINER="${DS_CONTAINER:-ds-standalone}"
IMAGE="apache/dolphinscheduler-standalone-server:3.2.2"
DS_HOME=/opt/dolphinscheduler
JAR_NAME="dolphinscheduler-task-lineage-3.2.2.jar"
JAR_LOCAL="$PLUGIN_DIR/$JAR_NAME"
PY="$ROOT/.venv/bin/python"
DS_BASE="http://localhost:12345/dolphinscheduler"
UI_URL="$DS_BASE/ui/"
LIB_DIRS=(worker-server master-server api-server standalone-server)

SKIP_VERIFY=0
SKIP_DATA=0
FORCE_RESTART=0
for a in "$@"; do
  case "$a" in
    --skip-verify) SKIP_VERIFY=1 ;;
    --skip-data) SKIP_DATA=1 ;;
    --force-restart) FORCE_RESTART=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数: $a"; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m━━━━ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✅ %s\033[0m\n' "$*"; }
warn() { printf '  \033[1;33m⚠️  %s\033[0m\n' "$*"; }
bad()  { printf '  \033[1;31m❌ %s\033[0m\n' "$*"; }
info() { printf '     %s\n' "$*"; }

NEEDS_RESTART=0
FAILED=0

# ---------------------------------------------------------------------------
step "0/7 前置检查"
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 && ok "docker 可用" || { bad "docker 不可用"; exit 1; }
docker info >/dev/null 2>&1 && ok "docker 守护进程正常" || { bad "docker 未运行（试试 bash /usr/local/bin/start-docker.sh）"; exit 1; }
[ -f "$PY" ] && ok "项目 venv 存在" || warn "未找到 $PY（数据重建会跳过）"
command -v javac >/dev/null 2>&1 && ok "javac 可用（$(javac -version 2>&1)）" || warn "无 javac（若本地已有 jar 则不影响）"

# ---------------------------------------------------------------------------
step "1/7 启动血缘解析服务（插件要调用它）"
# ---------------------------------------------------------------------------
if curl -s -m 5 http://localhost:18080/health >/dev/null 2>&1; then
  ok "血缘服务已在运行（18080）"
else
  if [ -x /usr/local/bin/start-lineage-api.sh ]; then
    bash /usr/local/bin/start-lineage-api.sh >/dev/null 2>&1 || true
  fi
  if curl -s -m 5 http://localhost:18080/health >/dev/null 2>&1; then
    ok "血缘服务已启动（18080）"
  else
    bad "血缘服务启动失败（插件运行时会连不上）"; FAILED=1
  fi
fi

# ---------------------------------------------------------------------------
step "2/7 确保海豚容器存在且运行"
# ---------------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  warn "容器不存在，用镜像 $IMAGE 新建"
  docker run -d --name "$CONTAINER" -p 12345:12345 -p 25333:25333 "$IMAGE" >/dev/null 2>&1 \
    && ok "容器已创建" || { bad "容器创建失败"; docker logs --tail 20 "$CONTAINER" 2>&1; exit 1; }
  NEEDS_RESTART=0   # 新建的容器马上会就绪，无需再重启
else
  state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  if [ "$state" != "running" ]; then
    warn "容器状态=$state，正在启动"
    docker start "$CONTAINER" >/dev/null 2>&1 && ok "已启动" || { bad "启动失败"; exit 1; }
  else
    ok "容器运行中"
  fi
fi

# ---------------------------------------------------------------------------
step "3/7 部署插件 jar（本地没有则先编译）"
# ---------------------------------------------------------------------------
if [ ! -f "$JAR_LOCAL" ]; then
  if command -v javac >/dev/null 2>&1; then
    info "本地无 jar，执行编译（ds-plugin/build.sh --no-restart）..."
    ( cd "$PLUGIN_DIR" && bash build.sh --no-restart ) >/tmp/prep_build.log 2>&1
    if [ -f "$JAR_LOCAL" ]; then
      ok "编译完成：$JAR_NAME ($(du -h "$JAR_LOCAL" | cut -f1))"
    else
      bad "编译失败，详见 /tmp/prep_build.log"; tail -15 /tmp/prep_build.log; FAILED=1
    fi
  else
    bad "本地无 jar 且无 javac，无法编译"; FAILED=1
  fi
else
  ok "本地已有 jar：$JAR_NAME ($(du -h "$JAR_LOCAL" | cut -f1))"
fi

if [ -f "$JAR_LOCAL" ]; then
  LOCAL_MD5="$(md5sum "$JAR_LOCAL" | cut -d' ' -f1)"
  for d in "${LIB_DIRS[@]}"; do
    REMOTE_MD5="$(docker exec "$CONTAINER" sh -c "md5sum $DS_HOME/libs/$d/$JAR_NAME 2>/dev/null | cut -d' ' -f1" 2>/dev/null)"
    if [ "$REMOTE_MD5" != "$LOCAL_MD5" ]; then
      docker cp "$JAR_LOCAL" "$CONTAINER:$DS_HOME/libs/$d/" >/dev/null 2>&1 \
        && info "已更新 libs/$d/$JAR_NAME" || { bad "复制到 $d 失败"; FAILED=1; }
      NEEDS_RESTART=1
    else
      info "libs/$d 已是最新"
    fi
  done
  [ "$NEEDS_RESTART" = "1" ] && ok "插件 jar 已同步（需重启海豚生效）" || ok "插件 jar 全部已是最新（无需重启）"
fi

# ---------------------------------------------------------------------------
step "4/7 部署 UI 配置（让 LINEAGE 出现在任务类型列表）"
# ---------------------------------------------------------------------------
UI_CFG_SRC="$PLUGIN_DIR/conf/dynamic-task-type-config.yaml"
UI_CFG_DST="$DS_HOME/conf/dynamic-task-type-config.yaml"
if [ -f "$UI_CFG_SRC" ]; then
  S_MD5="$(md5sum "$UI_CFG_SRC" | cut -d' ' -f1)"
  D_MD5="$(docker exec "$CONTAINER" sh -c "md5sum $UI_CFG_DST 2>/dev/null | cut -d' ' -f1")"
  if [ "$S_MD5" != "$D_MD5" ]; then
    docker exec "$CONTAINER" sh -c "cp -n $UI_CFG_DST ${UI_CFG_DST}.bak 2>/dev/null || true" >/dev/null 2>&1
    docker cp "$UI_CFG_SRC" "$CONTAINER:$UI_CFG_DST" >/dev/null 2>&1 && info "已更新 dynamic-task-type-config.yaml" || bad "配置复制失败"
    NEEDS_RESTART=1
  else
    info "dynamic-task-type-config.yaml 已是最新"
  fi
  # 表单 JSON / 图标
  if [ -d "$PLUGIN_DIR/ui-static/lineage" ]; then
    docker exec "$CONTAINER" mkdir -p "$DS_HOME/ui/static/lineage" >/dev/null 2>&1
    for f in "$PLUGIN_DIR"/ui-static/lineage/*; do
      bn="$(basename "$f")"
      f_md5="$(md5sum "$f" | cut -d' ' -f1)"
      c_md5="$(docker exec "$CONTAINER" sh -c "md5sum $DS_HOME/ui/static/lineage/$bn 2>/dev/null | cut -d' ' -f1")"
      if [ "$f_md5" != "$c_md5" ]; then
        docker cp "$f" "$CONTAINER:$DS_HOME/ui/static/lineage/$bn" >/dev/null 2>&1 && info "已更新 ui/static/lineage/$bn"
      fi
    done
    ok "UI 静态资源已同步"
  fi
else
  warn "未找到 conf/dynamic-task-type-config.yaml，跳过"
fi

# ---------------------------------------------------------------------------
step "5/7 重启海豚并等待就绪"
# ---------------------------------------------------------------------------
if [ "$FORCE_RESTART" = "1" ] || [ "$NEEDS_RESTART" = "1" ]; then
  [ "$FORCE_RESTART" = "1" ] && info "（--force-restart）强制重启" || info "有变更，重启生效"
  docker restart "$CONTAINER" >/dev/null 2>&1 && ok "已重启" || { bad "重启失败"; FAILED=1; }
else
  info "无变更，跳过重启（省时间）"
fi

info "等待海豚 UI 就绪 ..."
READY=0
for i in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$UI_URL" || true)"
  if [ "$code" = "200" ]; then
    ok "海豚已就绪（第 ${i} 次探测，HTTP $code）"
    READY=1
    break
  fi
  sleep 2
done
[ "$READY" = "1" ] || { bad "等待超时（docker logs $CONTAINER 查看）"; FAILED=1; }

# 插件注册日志（带重试：海豚启动后插件加载可能晚于 UI 就绪）
# 注意：这里不能用 grep -q —— set -o pipefail 下 grep -q 提前退出会让 docker logs 收到
# SIGPIPE（退出码 141），整个管道被判为失败，造成"插件没加载"的误报。
PLUGIN_OK=0
for i in $(seq 1 12); do
  if docker logs "$CONTAINER" 2>&1 | grep "Registered task plugin: LINEAGE" >/dev/null; then
    PLUGIN_OK=1
    ok "插件已加载：Registered task plugin: LINEAGE"
    break
  fi
  sleep 5
done
if [ "$PLUGIN_OK" != "1" ]; then
  bad "日志里没找到 LINEAGE 插件注册（检查 jar 是否部署）"
  FAILED=1
fi

# ---------------------------------------------------------------------------
step "6/7 重建演示数据（内存库重启后必做）"
# ---------------------------------------------------------------------------
if [ "$SKIP_DATA" = "1" ]; then
  warn "（--skip-data）跳过数据重建"
elif [ ! -f "$PY" ]; then
  warn "无 venv python，跳过"
else
  if curl -s -m 10 -X POST "$DS_BASE/login" -d 'userName=admin&userPassword=dolphinscheduler123' | grep '"success"' >/dev/null; then
    ok "海豚登录正常"
  else
    warn "海豚登录异常（继续尝试重建）"
  fi

  info "重建「烟草数仓演示」项目 + 4 个工作流（16 个任务）..."
  if "$PY" "$ROOT/scripts/ds_setup_demo.py" --no-export >/tmp/prep_demo_data.log 2>&1; then
    ok "烟草数仓演示数据已就绪"
    grep -E "项目|工作流|完成|✅" /tmp/prep_demo_data.log | tail -8 | sed 's/^/     /'
  else
    warn "ds_setup_demo.py 返回非 0（详情 /tmp/prep_demo_data.log）"
    tail -6 /tmp/prep_demo_data.log | sed 's/^/     /'
  fi
fi

# ---------------------------------------------------------------------------
step "7/7 端到端验证：LINEAGE 任务真跑一次"
# ---------------------------------------------------------------------------
if [ "$SKIP_VERIFY" = "1" ]; then
  warn "（--skip-verify）跳过验证"
elif [ ! -f "$PY" ]; then
  warn "无 venv python，跳过验证"
else
  if "$PY" "$PLUGIN_DIR/verify_api.py" > /tmp/prep_verify.log 2>&1; then
    ok "端到端验证通过（工作流 + LINEAGE 任务均 SUCCESS）"
    grep -E "workflowCode|taskType=LINEAGE|state=SUCCESS|最终任务实例|输入表|输出表|耗时" /tmp/prep_verify.log | tail -10 | sed 's/^/     /'
  else
    bad "端到端验证失败（详情 /tmp/prep_verify.log）"
    tail -12 /tmp/prep_verify.log | sed 's/^/     /'
    FAILED=1
  fi
fi

# ---------------------------------------------------------------------------
step "完成"
# ---------------------------------------------------------------------------
echo "  海豚 UI    : $UI_URL       （admin / dolphinscheduler123）"
echo "  血缘服务   : http://localhost:18080/health"
echo "  API 文档   : $DS_BASE"
echo
echo "  【演示路径】登录海豚 → 项目「血缘分析插件演示」→ 工作流「wf_lineage_血缘分析演示」"
echo "              → 运行 → 工作流实例 → 任务实例 → 查看日志（血缘报告在此）"
echo
if [ "$FAILED" = "1" ]; then
  bad "存在失败项，请查看上方输出"
  exit 1
fi
ok "演示环境已就绪 🎉"
