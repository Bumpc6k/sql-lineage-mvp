#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# LINEAGE 血缘分析插件 —— 部署 + 端到端验证（全部打真实请求，输出即证据）
#
#   1. 海豚 UI / 容器状态
#   2. 插件 jar 在容器内的位置（4 个 libs 目录）
#   3. TaskPluginManager 启动日志里的 LINEAGE 注册记录（证明插件被加载）
#   4. 容器访问宿主血缘服务 http://172.17.0.1:18080 的连通性
#   5. UI 用到的动态任务类型接口是否返回 LINEAGE
#   6. 端到端：建项目 -> 建 LINEAGE 工作流 -> 上线 -> 运行 -> 拉任务日志（血缘报告）
#
# 用法: bash verify.sh [--skip-run]      # --skip-run 只做 1~5，不跑工作流
# ---------------------------------------------------------------------------
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${DS_CONTAINER:-ds-standalone}"
JAR="dolphinscheduler-task-lineage-3.2.2.jar"
OUT_DIR="${PROJECT_DIR}/build"
mkdir -p "${OUT_DIR}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "1/6 海豚 UI 探活"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:12345/dolphinscheduler/ui || true)"
echo "GET http://localhost:12345/dolphinscheduler/ui -> HTTP ${code}"

log "2/6 插件 jar 在容器内（worker/master/api/standalone 四个 libs 目录）"
docker exec "${CONTAINER}" ls -lh \
  "/opt/dolphinscheduler/libs/worker-server/${JAR}" \
  "/opt/dolphinscheduler/libs/master-server/${JAR}" \
  "/opt/dolphinscheduler/libs/api-server/${JAR}" \
  "/opt/dolphinscheduler/libs/standalone-server/${JAR}"
echo "--- jar 内容 ---"
python3 - "$PROJECT_DIR/$JAR" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for n in z.namelist():
        if n.endswith('.class') or 'services' in n:
            print("   ", n)
PY

log "3/6 TaskPluginManager 启动日志（插件已被海豚加载）"
docker logs "${CONTAINER}" 2>&1 | grep -F "TaskPluginManager" | grep -i "LINEAGE" | tail -4

log "4/6 容器 -> 宿主血缘服务连通性"
docker exec "${CONTAINER}" curl -s --max-time 10 http://172.17.0.1:18080/health
echo

log "5/6 UI 动态任务类型接口（/dynamic/universal/taskTypes）"
python3 - <<'PY'
import http.cookiejar, json, urllib.parse, urllib.request
BASE = "http://localhost:12345/dolphinscheduler"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def call(m, p, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    with op.open(urllib.request.Request(BASE + p, data=body, method=m), timeout=60) as r:
        return json.loads(r.read().decode())
print("login:", call("POST", "/login", {"userName": "admin", "userPassword": "dolphinscheduler123"})["success"])
print("GET /dynamic/taskCategories ->",
      json.dumps(call("GET", "/dynamic/taskCategories")["data"], ensure_ascii=False))
d = call("GET", "/dynamic/Universal/taskTypes")
print("GET /dynamic/Universal/taskTypes ->", json.dumps(d["data"], ensure_ascii=False))
print("包含 LINEAGE:", any(t.get("name") == "LINEAGE" for t in d["data"]))
PY
echo "静态表单 JSON: $(curl -s -o /dev/null -w '%{http_code} %{content_type}' http://localhost:12345/dolphinscheduler/ui/static/lineage/lineage.json)"

if [ "${1:-}" = "--skip-run" ]; then
  log "跳过端到端运行 (--skip-run)"
  exit 0
fi

log "6/6 端到端：建工作流(taskType=LINEAGE) -> 上线 -> 运行 -> 任务日志"
python3 "${PROJECT_DIR}/verify_api.py" 2>&1 | tee "${OUT_DIR}/verify_output.txt"
echo
echo "完整输出已保存到 ${OUT_DIR}/verify_output.txt"
