#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DolphinScheduler LINEAGE 插件端到端验证（被 verify.sh 调用）。

做四件事，全部打真实 HTTP 请求并把响应原样打印：
  1. 登录海豚，创建（或复用）一个演示项目
  2. 用 taskType=LINEAGE 创建工作流定义  -> 证明插件已被海豚加载
  3. 上线 + 运行该工作流，轮询任务实例状态
  4. 拉取任务实例日志，打印其中的血缘报告
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:12345/dolphinscheduler"
USER = "admin"
PASSWORD = "dolphinscheduler123"

PROJECT_NAME = "血缘分析插件演示"
PROJECT_DESC = "DolphinScheduler 自定义 LINEAGE(血缘分析) 任务类型插件演示"
WORKFLOW_NAME = "wf_lineage_血缘分析演示"

DEMO_SQL = (
    "INSERT INTO dwd.t_order\n"
    "SELECT id, amt, dt\n"
    "FROM ods.t_order_src\n"
    "WHERE dt = '2026-01-01'"
)

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def call(method: str, path: str, params=None, data=None, timeout=60):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {raw[:600]}")
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw": raw[:2000]}


def step(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    step("0) 登录海豚")
    r = call("POST", "/login", data={"userName": USER, "userPassword": PASSWORD})
    print(json.dumps(r, ensure_ascii=False)[:300])
    if not r.get("success"):
        print("!! 登录失败")
        return 1

    step("1) 准备演示项目")
    proj = call("GET", "/projects", {"pageSize": 100, "pageNo": 1, "searchVal": PROJECT_NAME})
    total = proj.get("data", {}).get("total", 0)
    if total:
        project = proj["data"]["totalList"][0]
        print(f"复用已有项目: {project['name']} code={project['code']}")
    else:
        created = call("POST", "/projects", data={"projectName": PROJECT_NAME,
                                                  "description": PROJECT_DESC})
        print("创建项目响应:", json.dumps(created, ensure_ascii=False)[:300])
        project = created["data"]
    pc = int(project["code"])
    print(f"projectCode = {pc}")

    step("2) 创建工作流定义（唯一任务 taskType = LINEAGE）")
    # 同名工作流先删掉，保证脚本可重复执行
    existing = call("GET", f"/projects/{pc}/process-definition",
                    {"searchVal": WORKFLOW_NAME, "pageSize": 20, "pageNo": 1})
    for wf in (existing.get("data", {}) or {}).get("totalList", []) or []:
        if wf.get("name") == WORKFLOW_NAME:
            # 海豚要求先下线再删除
            call("POST", f"/projects/{pc}/process-definition/{wf['code']}/release", {"releaseState": "OFFLINE"})
            d = call("DELETE", f"/projects/{pc}/process-definition/{wf['code']}")
            print(f"删除同名旧工作流 code={wf['code']} -> {d.get('msg')}")

    codes = call("GET", f"/projects/{pc}/task-definition/gen-task-codes", {"genNum": 1})
    task_code = int(codes["data"][0])
    print(f"taskCode = {task_code}")

    task_params = {
        "mode": "sql",
        "sql": DEMO_SQL,
        "dialect": "hive",
        "table": "",
        "depth": 3,
        "graph": "warehouse_graph.json",
        "serviceUrl": "http://172.17.0.1:18080",
        "timeout": 30000,
        "localParams": [],
        "resourceList": [],
        "varPool": [],
    }
    task_definition = [{
        "code": task_code,
        "name": "t_lineage_订单血缘",
        "version": 1,
        "description": "调用血缘服务解析 SQL 血缘",
        "delayTime": 0,
        "taskType": "LINEAGE",
        "taskParams": task_params,
        "flag": "YES",
        "isCache": "NO",
        "taskPriority": "MEDIUM",
        "workerGroup": "default",
        "environmentCode": -1,
        "failRetryTimes": 0,
        "failRetryInterval": 1,
        "timeoutFlag": "CLOSE",
        "timeoutNotifyStrategy": "",
        "timeout": 0,
        "taskExecuteType": "BATCH",
    }]
    # DS 3.2.x 的 ProcessTaskRelation 结构（注意不是 conditionResult/switchResult）
    task_relation = [{
        "name": "",
        "preTaskCode": 0,
        "preTaskVersion": 0,
        "postTaskCode": task_code,
        "postTaskVersion": 1,
        "conditionType": "NONE",
        "conditionParams": {},
    }]
    locations = [{"taskCode": task_code, "x": 200, "y": 200}]

    payload = {
        "name": WORKFLOW_NAME,
        "description": "LINEAGE 插件端到端演示工作流",
        "globalParams": "[]",
        "locations": json.dumps(locations, ensure_ascii=False),
        "timeout": 0,
        "taskRelationJson": json.dumps(task_relation, ensure_ascii=False),
        "taskDefinitionJson": json.dumps(task_definition, ensure_ascii=False),
        "otherParamsJson": "",
        "executionType": "PARALLEL",
    }
    print("POST /projects/%d/process-definition" % pc)
    print("taskDefinitionJson =", payload["taskDefinitionJson"])
    created = call("POST", f"/projects/{pc}/process-definition", data=payload)
    print("响应:", json.dumps(created, ensure_ascii=False)[:900])
    if not created.get("success"):
        print("!! 创建工作流失败（如果同名工作流已存在，请先删除后重跑）")
        return 1
    wf_code = int(created["data"]["code"])
    print(f"workflowCode = {wf_code}")

    step("2b) 回读工作流定义，确认 taskType=LINEAGE 已落库")
    detail = call("GET", f"/projects/{pc}/process-definition/{wf_code}")
    d = detail.get("data", {})
    for t in d.get("taskDefinitionList", []):
        print(f"  任务 {t['name']}  taskType={t['taskType']}")
        print(f"  taskParams={t['taskParams']}")
    if not any(t["taskType"] == "LINEAGE" for t in d.get("taskDefinitionList", [])):
        print("!! 回读结果里没有 LINEAGE 任务")
        return 1

    step("3) 上线并运行工作流")
    # 注意 releaseState 用的是枚举名 ONLINE / OFFLINE，不是 1 / 0
    rel = call("POST", f"/projects/{pc}/process-definition/{wf_code}/release", {"releaseState": "ONLINE"})
    print("上线响应:", json.dumps(rel, ensure_ascii=False)[:300])

    start = call("POST", f"/projects/{pc}/executors/start-process-instance", {
        "processDefinitionCode": wf_code,
        "scheduleTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "failureStrategy": "CONTINUE",
        "execType": "START_PROCESS",
        "warningType": "NONE",
        "runMode": "RUN_MODE_SERIAL",
        "processInstancePriority": "MEDIUM",
        "workerGroup": "default",
        "tenantCode": "default",
        "environmentCode": -1,
        "startParams": "",
        "dryRun": 0,
        "testFlag": 0,
    })
    print("启动响应:", json.dumps(start, ensure_ascii=False)[:500])

    step("4) 轮询任务实例状态 + 拉取任务日志")
    task_instance_id = None
    state = None
    for i in range(40):
        time.sleep(3)
        inst = call("GET", f"/projects/{pc}/process-instances",
                    {"processDefineCode": wf_code, "pageSize": 5, "pageNo": 1})
        lst = inst.get("data", {}).get("totalList", [])
        if not lst:
            continue
        pi = lst[0]
        tasks = call("GET", f"/projects/{pc}/process-instances/{pi['id']}/tasks")
        tl = tasks.get("data", {}).get("taskList", [])
        if not tl:
            continue
        t = tl[0]
        task_instance_id = t["id"]
        state = t["state"]
        print(f"   [{i}] 工作流实例 {pi['id']} state={pi['state']} | 任务实例 {t['id']} "
              f"state={t['state']} taskType={t.get('taskType')}")
        if state in ("SUCCESS", "FAILURE", "KILL", "NEED_FAULT_TOLERANCE", "FORCED_SUCCESS"):
            break

    if task_instance_id is None:
        print("!! 未拿到任务实例")
        return 1

    step("5) 任务实例日志（含血缘报告）")
    log = call("GET", "/log/detail", {"taskInstanceId": task_instance_id,
                                      "skipLineNum": 0, "limit": 2000})
    msg = log.get("data", {}).get("message") or json.dumps(log, ensure_ascii=False)[:2000]
    print(msg)

    print()
    print(f"最终任务实例 {task_instance_id} 状态: {state}")
    return 0 if state == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
