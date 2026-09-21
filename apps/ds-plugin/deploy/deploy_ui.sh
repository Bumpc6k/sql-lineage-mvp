#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 让「血缘分析 / LINEAGE」出现在海豚 UI 的任务类型列表里。
#
# 海豚 3.2.2 的前端把标准任务类型的别名映射写死在前端 bundle 里（task-type.*.js 的
# 常量表），后端新加的 SPI 插件在前端没有对应 Vue 组件。海豚为此留了口子：
#   - conf/dynamic-task-type-config.yaml  : 声明动态任务类型（名称 / 图标 / 表单 JSON）
#   - 后端 /dolphinscheduler/dynamic/{category}/taskTypes 把配置吐给前端
#   - UI 的 DAG 侧边栏拉取该接口并渲染可拖拽的任务项
# 本脚本把配置和表单 JSON 拷进容器（容器内文件不持久，源文件都在本仓库里）。
#
# 用法: bash deploy_ui.sh
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${DS_CONTAINER:-ds-standalone}"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

log "1/3 备份容器内原 dynamic-task-type-config.yaml"
docker exec "${CONTAINER}" sh -c \
  'cp -n /opt/dolphinscheduler/conf/dynamic-task-type-config.yaml /opt/dolphinscheduler/conf/dynamic-task-type-config.yaml.bak 2>/dev/null; ls -l /opt/dolphinscheduler/conf/dynamic-task-type-config.yaml*'

log "2/3 拷贝动态任务类型配置 + 表单 JSON / 图标"
docker cp "${PROJECT_DIR}/../frontend/conf/dynamic-task-type-config.yaml" \
  "${CONTAINER}:/opt/dolphinscheduler/conf/dynamic-task-type-config.yaml"
docker exec "${CONTAINER}" mkdir -p /opt/dolphinscheduler/ui/static/lineage
docker cp "${PROJECT_DIR}/../frontend/ui-static/lineage/." "${CONTAINER}:/opt/dolphinscheduler/ui/static/lineage/"
docker exec "${CONTAINER}" sh -c 'ls -l /opt/dolphinscheduler/ui/static/lineage/'

log "3/3 重启海豚使配置生效"
docker restart "${CONTAINER}"
for i in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:12345/dolphinscheduler/ui || true)"
  if [ "${code}" = "200" ]; then
    echo "   海豚 UI 已就绪 (第 ${i} 次探测)"
    exit 0
  fi
  sleep 2
done
echo "!! 等待超时" >&2
exit 1
