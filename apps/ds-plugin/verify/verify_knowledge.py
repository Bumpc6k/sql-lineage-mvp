#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DolphinScheduler LINEAGE 插件「血缘 + 业务口径」端到端验证（真实 HTTP，无 mock）。

与 verify_api.py 的区别：SQL 用 ``examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql``，
它产出的核心指标「产量」在知识库里有口径，因此任务日志第 ⑤ 段应当能打印出：
    产量（chanliang_qty） = 打码量 + 跳码量 - 重码量

流程：登录 → 建/复用项目 → 建 LINEAGE 工作流 → 上线 → 运行 → 轮询任务实例 → 拉日志。
用法：.venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://localhost:12345/dolphinscheduler"
USER = "admin"
PASSWORD = "dolphinscheduler123"

PROJECT_NAME = "血缘分析插件演示"
PROJECT_DESC = "DolphinScheduler 自定义 LINEAGE(血缘分析) 任务类型插件演示"
#: 可用环境变量覆盖，方便跑「旧版服务降级」这一路验证
WORKFLOW_NAME = os.environ.get("LINEAGE_WF_NAME", "wf_lineage_业务口径演示")
TASK_NAME = "t_lineage_产量口径"
SERVICE_URL = os.environ.get("LINEAGE_SERVICE_URL", "http://172.17.0.1:18080")
SQL_FILE = Path(__file__).resolve().parents[1] / "examples" / "knowledge_demo" / "cdw" / "dwd_卷烟产量码段明细.sql"

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
    ap = argparse.ArgumentParser(description="LINEAGE 插件「血缘 + 业务口径」端到端验证")
    ap.add_argument("--degraded", action="store_true",
                    help="验证降级路径：期望日志里出现「回退到 /parse」与「未匹配到业务口径」")
    args = ap.parse_args()

    step("0) 登录海豚")
    r = call("POST", "/login", data={"userName": USER, "userPassword": PASSWORD})
    print(json.dumps(r, ensure_ascii=False)[:200])
    if not r.get("success"):
        print("!! 登录失败")
        return 1

    step("1) 准备项目")
    proj = call("GET", "/projects", {"pageSize": 100, "pageNo": 1, "searchVal": PROJECT_NAME})
    if proj.get("data", {}).get("total"):
        project = proj["data"]["totalList"][0]
        print(f"复用已有项目: {project['name']} code={project['code']}")
    else:
        created = call("POST", "/projects", data={"projectName": PROJECT_NAME, "description": PROJECT_DESC})
        print("创建项目响应:", json.dumps(created, ensure_ascii=False)[:300])
        project = created["data"]
    pc = int(project["code"])

    sql_text = SQL_FILE.read_text(encoding="utf-8")
    step(f"2) 用真实口径 SQL 建 LINEAGE 工作流（{SQL_FILE.name}，{len(sql_text.splitlines())} 行）")

    existing = call("GET", f"/projects/{pc}/process-definition",
                    {"searchVal": WORKFLOW_NAME, "pageSize": 20, "pageNo": 1})
    for wf in (existing.get("data", {}) or {}).get("totalList", []) or []:
        if wf.get("name") == WORKFLOW_NAME:
            call("POST", f"/projects/{pc}/process-definition/{wf['code']}/release", {"releaseState": "OFFLINE"})
            d = call("DELETE", f"/projects/{pc}/process-definition/{wf['code']}")
            print(f"删除同名旧工作流 code={wf['code']} -> {d.get('msg')}")

    task_code = int(call("GET", f"/projects/{pc}/task-definition/gen-task-codes", {"genNum": 1})["data"][0])
    print(f"taskCode = {task_code}")

    task_params = {
        "mode": "sql",
        "sql": sql_text,
        "dialect": "hive",
        "table": "",
        "depth": 3,
        "graph": "warehouse_graph.json",
        "serviceUrl": SERVICE_URL,
        "timeout": 30000,
        "localParams": [],
        "resourceList": [],
        "varPool": [],
    }
    task_definition = [{
        "code": task_code,
        "name": TASK_NAME,
        "version": 1,
        "description": "解析码段产量 SQL 血缘 + 匹配产量口径",
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
    task_relation = [{
        "name": "",
        "preTaskCode": 0,
        "preTaskVersion": 0,
        "postTaskCode": task_code,
        "postTaskVersion": 1,
        "conditionType": "NONE",
        "conditionParams": {},
    }]
    payload = {
        "name": WORKFLOW_NAME,
        "description": "LINEAGE 插件：血缘 + 业务口径一体化演示",
        "globalParams": "[]",
        "locations": json.dumps([{"taskCode": task_code, "x": 200, "y": 200}], ensure_ascii=False),
        "timeout": 0,
        "taskRelationJson": json.dumps(task_relation, ensure_ascii=False),
        "taskDefinitionJson": json.dumps(task_definition, ensure_ascii=False),
        "otherParamsJson": "",
        "executionType": "PARALLEL",
    }
    created = call("POST", f"/projects/{pc}/process-definition", data=payload)
    print("创建工作流响应:", json.dumps(created, ensure_ascii=False)[:300])
    if not created.get("success"):
        print("!! 创建工作流失败")
        return 1
    wf_code = int(created["data"]["code"])
    print(f"workflowCode = {wf_code}")

    step("3) 上线并运行")
    rel = call("POST", f"/projects/{pc}/process-definition/{wf_code}/release", {"releaseState": "ONLINE"})
    print("上线响应:", json.dumps(rel, ensure_ascii=False)[:200])
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
    print("启动响应:", json.dumps(start, ensure_ascii=False)[:300])
    if not start.get("success"):
        print("!! 启动失败")
        return 1

    step("4) 轮询任务实例")
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

    step("5) 任务实例日志原文（血缘报告 + 业务口径）")
    log = call("GET", "/log/detail", {"taskInstanceId": task_instance_id,
                                      "skipLineNum": 0, "limit": 40000})
    msg = log.get("data", {}).get("message") or ""
    if not msg:
        print(json.dumps(log, ensure_ascii=False)[:2000])
        return 1
    print(msg)

    step("6) 断言检查")
    if args.degraded:
        checks = [
            ("① 表级血缘", "① 表级血缘" in msg),
            ("② 字段级血缘", "② 字段级血缘" in msg),
            ("③ 加工条件", "③ 加工条件" in msg),
            ("④ 加工 SQL 原文", "④ 加工 SQL 原文" in msg),
            ("⑤ 业务口径段仍在（降级提示）", "未匹配到业务口径（可先执行 kb build 建库）" in msg),
            ("日志写明回退 /parse", "回退到 http://172.17.0.1:18099/parse" in msg),
            ("汇总行仍输出", "血缘分析完成" in msg),
            ("未命中口径计数为 0", "业务口径命中 0 条" in msg),
            ("降级路径不输出报告 URL 行（安静略过）", "📊 完整报告" not in msg),
            ("② 字段级仍是紧凑表格", "目标字段" in msg and "加工表达式" in msg and "┼" in msg),
        ]
    else:
        checks = [
            ("① 表级血缘", "① 表级血缘" in msg),
            ("② 字段级血缘", "② 字段级血缘" in msg),
            ("③ 加工条件", "③ 加工条件" in msg),
            ("④ 加工 SQL 原文", "④ 加工 SQL 原文" in msg),
            ("⑤ 业务口径", "⑤ 业务口径（知识库匹配）" in msg),
            ("产量口径公式原文", "产量（chanliang_qty） = 打码量 + 跳码量 - 重码量" in msg),
            ("上游链路", "cdw.dwd_卷烟产量码段明细 → ods.ods_卷烟码段流水" in msg),
            ("字段中文名", "dama_qty → 打码量" in msg),
            ("汇总行口径命中数", re.search(r"业务口径命中 \d+ 条", msg) is not None),
            ("varPool 新增口径出参", "lineage_metric_count" in msg and "lineage_metric_names" in msg),
            # --- 本轮「日志精简 + 可跳转 HTML 报告」新增断言 ---
            ("② 字段级是紧凑表格（表头 + 分隔线）",
             "目标字段" in msg and "来源字段" in msg and "加工表达式" in msg and "┼" in msg),
            ("② 超出 15 行折叠成一行提示", "其余 2 行见完整报告" in msg),
            ("⑤ 只展开最关键的 3 条口径", len(re.findall(r"│\s+★ \d\.", msg)) == 3),
            ("⑤ 其余口径折叠成一行", "另有 4 条口径" in msg and "详见完整报告" in msg),
            ("📊 完整报告 URL 行（可点击）",
             re.search(r"📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_\d{8}_\d{6}_[0-9a-f]{8}", msg) is not None),
            ("varPool 新增报告出参",
             "lineage_report_id" in msg and "lineage_report_url" in msg),
        ]
    for name, ok in checks:
        print(f"   {'✅' if ok else '❌'} {name}")

    m = re.search(r"业务口径命中 (\d+) 条", msg)
    print(f"\n最终任务实例 {task_instance_id} 状态: {state}，口径命中 {m.group(1) if m else '?'} 条")
    url = re.search(r"📊 完整报告（浏览器打开）: (\S+)", msg)
    if url:
        print(f"任务日志里的完整报告地址: {url.group(1)}")
        try:
            req = urllib.request.Request(url.group(1)  # 宿主机视角的 localhost 地址
                                         .replace("http://localhost:", "http://127.0.0.1:"))
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
            card_marker = '<div class="card">'
            print(f"   ✅ 报告可打开：HTTP {resp.status} {resp.headers.get('Content-Type')} "
                  f"{len(body.encode('utf-8'))} 字节，"
                  f"字段映射行 {body.count('<tr data-key=')} / 口径卡片 {body.count(card_marker)}")
        except Exception as e:  # noqa: BLE001 — 报告取不到不影响任务本身成功
            print(f"   ⚠️ 报告取回失败（不影响任务成功）: {type(e).__name__}: {e}")
    return 0 if (state == "SUCCESS" and all(ok for _, ok in checks)) else 1


if __name__ == "__main__":
    sys.exit(main())
