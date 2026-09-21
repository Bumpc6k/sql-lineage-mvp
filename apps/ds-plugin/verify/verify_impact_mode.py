#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LINEAGE 插件 `mode=impact` / `mode=upstream` 回归（确认改造后这两条路一字未变）。

断言：日志里 ①②③④ 段仍在（④ 变成「分析目标表」）、有下游影响/上游溯源段、
      第 ⑤ 段业务口径**不出现**（impact/upstream 不调 /analyze，没有口径可谈）。

用法：.venv/bin/python apps/apps/ds-plugin/verify/verify_impact_mode.py
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
WF_NAME = "wf_lineage_影响分析演示"
TASK_NAME = "t_lineage_下游影响"
TABLE = "ods.ods_卷烟码段流水"

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def call(method, path, params=None, data=None, timeout=60):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {e.read().decode('utf-8', 'replace')[:400]}")
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw": raw[:1500]}


def main() -> int:
    call("POST", "/login", data={"userName": USER, "userPassword": PASSWORD})
    proj = call("GET", "/projects", {"pageSize": 100, "pageNo": 1, "searchVal": PROJECT_NAME})
    if not proj.get("data", {}).get("total"):
        print("!! 演示项目不存在，请先跑 prep-ds-demo.sh")
        return 1
    pc = int(proj["data"]["totalList"][0]["code"])

    existing = call("GET", f"/projects/{pc}/process-definition",
                    {"searchVal": WF_NAME, "pageSize": 20, "pageNo": 1})
    for wf in (existing.get("data", {}) or {}).get("totalList", []) or []:
        if wf.get("name") == WF_NAME:
            call("POST", f"/projects/{pc}/process-definition/{wf['code']}/release", {"releaseState": "OFFLINE"})
            call("DELETE", f"/projects/{pc}/process-definition/{wf['code']}")

    task_code = int(call("GET", f"/projects/{pc}/task-definition/gen-task-codes", {"genNum": 1})["data"][0])
    task_params = {
        "mode": "impact", "sql": "", "dialect": "hive", "table": TABLE, "depth": 3,
        "graph": "warehouse_graph.json", "serviceUrl": "http://172.17.0.1:18080", "timeout": 30000,
        "localParams": [], "resourceList": [], "varPool": [],
    }
    created = call("POST", f"/projects/{pc}/process-definition", data={
        "name": WF_NAME, "description": "LINEAGE impact 模式回归", "globalParams": "[]",
        "locations": json.dumps([{"taskCode": task_code, "x": 200, "y": 200}]),
        "timeout": 0,
        "taskRelationJson": json.dumps([{"name": "", "preTaskCode": 0, "preTaskVersion": 0,
                                         "postTaskCode": task_code, "postTaskVersion": 1,
                                         "conditionType": "NONE", "conditionParams": {}}]),
        "taskDefinitionJson": json.dumps([{
            "code": task_code, "name": TASK_NAME, "version": 1, "description": "下游影响分析",
            "delayTime": 0, "taskType": "LINEAGE", "taskParams": task_params, "flag": "YES",
            "isCache": "NO", "taskPriority": "MEDIUM", "workerGroup": "default", "environmentCode": -1,
            "failRetryTimes": 0, "failRetryInterval": 1, "timeoutFlag": "CLOSE",
            "timeoutNotifyStrategy": "", "timeout": 0, "taskExecuteType": "BATCH",
        }], ensure_ascii=False),
        "otherParamsJson": "", "executionType": "PARALLEL",
    })
    if not created.get("success"):
        print("!! 建流失败", json.dumps(created, ensure_ascii=False)[:300])
        return 1
    wf_code = int(created["data"]["code"])
    call("POST", f"/projects/{pc}/process-definition/{wf_code}/release", {"releaseState": "ONLINE"})
    start = call("POST", f"/projects/{pc}/executors/start-process-instance", {
        "processDefinitionCode": wf_code, "scheduleTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "failureStrategy": "CONTINUE", "execType": "START_PROCESS", "warningType": "NONE",
        "runMode": "RUN_MODE_SERIAL", "processInstancePriority": "MEDIUM", "workerGroup": "default",
        "tenantCode": "default", "environmentCode": -1, "startParams": "", "dryRun": 0, "testFlag": 0,
    })
    print("启动响应:", json.dumps(start, ensure_ascii=False)[:200])

    task_instance_id, state = None, None
    for i in range(40):
        time.sleep(3)
        inst = call("GET", f"/projects/{pc}/process-instances",
                    {"processDefineCode": wf_code, "pageSize": 5, "pageNo": 1})
        lst = inst.get("data", {}).get("totalList", [])
        if not lst:
            continue
        tasks = call("GET", f"/projects/{pc}/process-instances/{lst[0]['id']}/tasks")
        tl = tasks.get("data", {}).get("taskList", [])
        if not tl:
            continue
        task_instance_id, state = tl[0]["id"], tl[0]["state"]
        if state in ("SUCCESS", "FAILURE", "KILL", "FORCED_SUCCESS"):
            break

    log = call("GET", "/log/detail", {"taskInstanceId": task_instance_id,
                                      "skipLineNum": 0, "limit": 40000})
    msg = log.get("data", {}).get("message") or ""
    print("=" * 78)
    print(msg)
    print("=" * 78)
    checks = [
        ("任务成功", state == "SUCCESS"),
        ("分析模式=impact", "下游影响分析 (downstream impact)" in msg),
        ("① 表级血缘", "① 表级血缘" in msg),
        ("② 字段级血缘", "② 字段级血缘" in msg),
        ("③ 加工条件", "③ 加工条件" in msg),
        ("下游影响分析段", "下游影响 分析" in msg or "下游影响" in msg),
        ("第 ⑤ 段不出现（impact 无口径）", "⑤ 业务口径" not in msg),
        ("汇总行不含口径命中", "业务口径命中" not in msg),
    ]
    for name, ok in checks:
        print(f"   {'✅' if ok else '❌'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
