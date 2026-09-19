#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 一键构建 + 部署 DolphinScheduler LINEAGE(血缘分析) 任务插件
#
#   1. 从 ds-standalone 容器导出编译所需 jar 到 ./libs/
#   2. javac --release 8 编译源码
#   3. 打包 jar（含 META-INF/services 的 SPI 注册文件）
#   4. docker cp 进容器的 worker-server / master-server / api-server / standalone-server
#   5. docker restart ds-standalone 并等待 UI 返回 200
#
# 用法: bash build.sh [--no-restart]
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${DS_CONTAINER:-ds-standalone}"
PLUGIN_JAR_NAME="dolphinscheduler-task-lineage-3.2.2.jar"
LIB_DIR="${PROJECT_DIR}/libs"
BUILD_DIR="${PROJECT_DIR}/build"
SRC_DIR="${PROJECT_DIR}/src/main/java"
RES_DIR="${PROJECT_DIR}/src/main/resources"
DS_LIBS_DIR="/opt/dolphinscheduler/libs"
# standalone 把 api/master/worker 合到一个进程，classpath 里四个 libs 目录都在
TARGET_LIB_DIRS=("worker-server" "master-server" "api-server" "standalone-server")

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

mkdir -p "${LIB_DIR}" "${BUILD_DIR}"

# ---------------------------------------------------------------------------
log "1/5 从容器导出编译依赖 jar"
# ---------------------------------------------------------------------------
export_jar() {  # export_jar <匹配前缀>
  local prefix="$1"
  if ls "${LIB_DIR}/${prefix}"*.jar >/dev/null 2>&1; then
    return 0
  fi
  local found
  found="$(docker exec "${CONTAINER}" sh -c "ls ${DS_LIBS_DIR}/api-server/${prefix}*.jar 2>/dev/null | head -1")"
  if [ -z "${found}" ]; then
    echo "!! 找不到依赖 jar: ${prefix}*.jar" >&2
    return 1
  fi
  docker cp "${CONTAINER}:${found}" "${LIB_DIR}/"
  echo "   exported $(basename "${found}")"
}

export_jar dolphinscheduler-task-api-
export_jar dolphinscheduler-spi-
export_jar dolphinscheduler-common-
export_jar jackson-databind-
export_jar jackson-core-
export_jar jackson-annotations-
export_jar slf4j-api-
export_jar commons-collections4-
export_jar commons-lang3-

# ---------------------------------------------------------------------------
log "2/5 javac --release 8 编译"
# ---------------------------------------------------------------------------
rm -rf "${BUILD_DIR}/classes"
mkdir -p "${BUILD_DIR}/classes"

CP="$(find "${LIB_DIR}" -name '*.jar' | tr '\n' ':')"
echo "   classpath: ${CP}"

mapfile -t SOURCES < <(find "${SRC_DIR}" -name '*.java' | sort)
echo "   sources (${#SOURCES[@]}):"
printf '     %s\n' "${SOURCES[@]#${PROJECT_DIR}/}"

javac --release 8 -encoding UTF-8 -Xlint:-options -cp "${CP}" -d "${BUILD_DIR}/classes" "${SOURCES[@]}"
echo "   javac 成功"

# ---------------------------------------------------------------------------
log "3/5 打包插件 jar (含 META-INF/services SPI 注册)"
# ---------------------------------------------------------------------------
cp -r "${RES_DIR}/." "${BUILD_DIR}/classes/"
echo "   SPI 注册文件内容: $(cat "${BUILD_DIR}/classes/META-INF/services/org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory")"

rm -f "${PROJECT_DIR}/${PLUGIN_JAR_NAME}"
( cd "${BUILD_DIR}/classes" && jar cf "${PROJECT_DIR}/${PLUGIN_JAR_NAME}" . )
echo "   产物: ${PROJECT_DIR}/${PLUGIN_JAR_NAME}"
jar tf "${PROJECT_DIR}/${PLUGIN_JAR_NAME}"

# ---------------------------------------------------------------------------
log "4/5 复制插件 jar 进容器"
# ---------------------------------------------------------------------------
for d in "${TARGET_LIB_DIRS[@]}"; do
  docker cp "${PROJECT_DIR}/${PLUGIN_JAR_NAME}" "${CONTAINER}:${DS_LIBS_DIR}/${d}/"
  echo "   -> ${DS_LIBS_DIR}/${d}/${PLUGIN_JAR_NAME}"
done

if [ "${1:-}" = "--no-restart" ]; then
  log "跳过重启 (--no-restart)"
  exit 0
fi

# ---------------------------------------------------------------------------
log "5/5 重启 ds-standalone 并等待 UI 就绪"
# ---------------------------------------------------------------------------
docker restart "${CONTAINER}"

for i in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:12345/dolphinscheduler/ui || true)"
  if [ "${code}" = "200" ]; then
    echo "   海豚 UI 已就绪 (第 ${i} 次探测, HTTP ${code})"
    echo
    echo "插件日志检查:"
    docker logs "${CONTAINER}" 2>&1 | grep -F "TaskPluginManager" | tail -5 || true
    exit 0
  fi
  sleep 2
done

echo "!! 等待超时，请检查 docker logs ${CONTAINER}" >&2
exit 1
