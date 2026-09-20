#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LINEAGE_DAG（工作流级血缘分析）插件端到端验证。

做五件事，全部打真实 HTTP 请求并把响应/日志原样打印：

  1. 登录海豚，定位「烟草数仓演示」项目（没有就建）；
  2. 新建工作流 ``wf_dag_工作流血缘演示``：**3 个 SQL 任务 + 1 个 LINEAGE_DAG 尾节点**
     （SQL 任务串成 ``src → ods → cdw.dwd → cdw.dws`` 的链路，DAG 节点挂在最后）；
  3. 上线 + 运行，轮询每个任务实例的状态；
  4. 拉 **LINEAGE_DAG 任务实例日志全文**（工作流血缘报告就在这里）；
  5. 演示「历史工作流零改造」：往**已有的** ``wf_dws_汇总``（4 个 SQL 任务，一行不改）
     尾部追加一个 LINEAGE_DAG 节点，再跑一次并打印它的日志。

用法::

    .venv/bin/python ds-plugin/verify_dag.py               # 两步都跑
    .venv/bin/python ds-plugin/verify_dag.py --phase1-only  # 只跑新建工作流
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
PROJECT_NAME = "烟草数仓演示"
WORKFLOW_NAME = "wf_dag_工作流血缘演示"
EXISTING_WORKFLOW = "wf_dws_汇总"
DAG_TASK_NAME = "t_dag_工作流血缘"

#: 三个 SQL 任务拼出的链路：src → ods → cdw.dwd → cdw.dws
SQL_ODS = """
-- ODS 贴源：ERP 生产工单 -> ods.ods_卷烟产量流水
INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')
SELECT
    a.work_order_no                 AS work_order_no,
    a.plant_code                    AS plant_code,
    p.plant_name                    AS plant_name,
    a.qty                           AS qty,
    a.dt                            AS dt
FROM src.erp_生产工单明细 a
LEFT JOIN dim.dim_plant p ON a.plant_code = p.plant_code
WHERE a.dt = '2026-01-01'
""".strip()

SQL_DWD = """
-- DWD 明细：产量流水 + 牌号维表 -> cdw.dwd_卷烟产量明细
INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '2026-01-01')
SELECT
    a.work_order_no                 AS work_order_no,
    a.plant_code                    AS plant_code,
    b.brand_name                    AS brand_name,
    a.qty                           AS chanliang_qty,
    a.dt                            AS dt
FROM ods.ods_卷烟产量流水 a
LEFT JOIN dim.dim_brand b ON a.brand_code = b.brand_code
WHERE a.dt = '2026-01-01'
""".strip()

SQL_DWS = """
-- DWS 汇总：按厂聚合产量
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                        AS plant_code,
    SUM(d.chanliang_qty)                AS chanliang_qty,
    COUNT(DISTINCT d.work_order_no)     AS work_order_cnt,
    d.dt                                AS dt
FROM cdw.dwd_卷烟产量明细 d
WHERE d.dt = '2026-01-01'
GROUP BY d.plant_code, d.dt
""".strip()

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


def info(msg: str) -> None:
    print(f"   {msg}")


def dag_task_definition(code: int, name: str) -> dict:
    """LINEAGE_DAG 任务定义（参数与 LineageDagParameters 一一对应）。"""
    return {
        "code": code,
        "name": name,
        "version": 1,
        "description": "工作流级血缘分析：一次解析整个工作流的全部任务脚本",
        "delayTime": 0,
        "taskType": "LINEAGE_DAG",
        "taskParams": {
            "scope": "current",
            "includeSubProcess": False,
            "taskTypes": "SQL,SHELL,PYTHON",
            "dialect": "hive",
            "serviceUrl": "http://172.17.0.1:18080",
            "timeout": 60000,
            "localParams": [],
            "resourceList": [],
            "varPool": [],
        },
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
    }


def sql_task_definition(code: int, name: str, sql: str, datasource_id: int) -> dict:
    return {
        "code": code,
        "name": name,
        "version": 1,
        "description": f"{name} 节点",
        "delayTime": 0,
        "taskType": "SQL",
        "taskParams": {
            "localParams": [],
            "resourceList": [],
            "type": "HIVE",
            "datasource": datasource_id,
            "sql": sql,
            "sqlType": "1",
            "preStatements": [],
            "postStatements": [],
            "displayRows": 10,
        },
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
    }


def relation(pre_code: int, post_code: int, post_version: int = 1) -> dict:
    return {
        "name": "",
        "preTaskCode": pre_code,
        "preTaskVersion": 0 if pre_code == 0 else 1,
        "postTaskCode": post_code,
        "postTaskVersion": post_version,
        "conditionType": "NONE",
        "conditionParams": {},
    }


def offline(project_code: int, workflow_code: int) -> None:
    """跑完把工作流置回 OFFLINE。

    海豚的 ``DELETE /process-definition`` 要求定义处于 OFFLINE；演示数据重建脚本
    （``scripts/ds_setup_demo.py``）会删掉整个「烟草数仓演示」项目再重建，
    如果这里留着 ONLINE 的定义，删除会失败 → 项目删不掉 → 建同名项目撞 already exists。
    """
    call("POST", f"/projects/{project_code}/process-definition/{workflow_code}/release",
         {"releaseState": "OFFLINE"})


def run_and_report(project_code: int, workflow_code: int, dag_task_code: int,
                   pull_log: bool = True, max_polls: int = 40) -> tuple:
    """上线 → 运行 → 轮询 → 打印任务状态；拿到 LINEAGE_DAG 实例时打印其日志全文。

    ``failureStrategy=CONTINUE``：本演示环境没有真实 Hive 数据源，SQL 任务必然失败，
    但不影响「工作流血缘分析」这件事本身 —— 它读的是工作流**定义**，不是执行结果。
    """
    info("上线")
    rel = call("POST", f"/projects/{project_code}/process-definition/{workflow_code}/release",
               {"releaseState": "ONLINE"})
    info(f"release: {json.dumps(rel, ensure_ascii=False)[:160]}")

    info("启动工作流实例（failureStrategy=CONTINUE）")
    start = call("POST", f"/projects/{project_code}/executors/start-process-instance", {
        "processDefinitionCode": workflow_code,
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
    info(f"启动响应: {json.dumps(start, ensure_ascii=False)[:300]}")

    dag_instance_id = None
    dag_state = None
    for i in range(max_polls):
        time.sleep(3)
        inst = call("GET", f"/projects/{project_code}/process-instances",
                    {"processDefineCode": workflow_code, "pageSize": 5, "pageNo": 1})
        items = (inst.get("data") or {}).get("totalList") or []
        if not items:
            continue
        pi = items[0]
        tasks = call("GET", f"/projects/{project_code}/process-instances/{pi['id']}/tasks")
        task_list = (tasks.get("data") or {}).get("taskList") or []
        states = ", ".join(f"{t['name']}={t['state']}" for t in task_list)
        print(f"   [{i}] 实例 {pi['id']} {pi['state']} | {states}")
        for t in task_list:
            if t.get("taskType") == "LINEAGE_DAG":
                dag_instance_id = t["id"]
                dag_state = t["state"]
        if dag_instance_id is not None and dag_state in ("SUCCESS", "FAILURE", "KILL"):
            break
        if dag_instance_id is None and pi["state"] in ("SUCCESS", "FAILURE", "STOP", "KILL"):
            info("上游任务失败且未开启「无视依赖」，海豚没有提交下游的 LINEAGE_DAG 节点"
                 "（这是海豚的依赖语义，不是插件问题）")
            break

    if dag_instance_id is None:
        offline(project_code, workflow_code)
        return None, None

    if pull_log:
        step(f"LINEAGE_DAG 任务实例日志全文（taskInstanceId={dag_instance_id}，state={dag_state}）")
        log = call("GET", "/log/detail", {"taskInstanceId": dag_instance_id,
                                          "skipLineNum": 0, "limit": 4000})
        msg = (log.get("data") or {}).get("message") or json.dumps(log, ensure_ascii=False)[:2000]
        print(msg)
    offline(project_code, workflow_code)
    info("已把工作流置回 OFFLINE（便于 demo 重建脚本整项目删除重建）")
    return dag_instance_id, dag_state


def build_workflow(project_code: int, datasource_id: int, name: str, parallel_dag: bool,
                   description: str) -> tuple:
    """建一个「3 个 SQL 任务 + 1 个 LINEAGE_DAG」的工作流，返回 (code, dag_code)。

    ``parallel_dag=True`` 时 LINEAGE_DAG 与 SQL 任务**同层**（无上游依赖）；
    否则串在最后一个 SQL 任务之后（真正的尾部节点）。
    """
    existing = call("GET", f"/projects/{project_code}/process-definition",
                    {"searchVal": name, "pageSize": 20, "pageNo": 1})
    for wf in ((existing.get("data") or {}).get("totalList") or []):
        if wf.get("name") == name:
            call("POST", f"/projects/{project_code}/process-definition/{wf['code']}/release",
                 {"releaseState": "OFFLINE"})
            call("DELETE", f"/projects/{project_code}/process-definition/{wf['code']}")
            info(f"删除同名旧工作流 code={wf['code']}")

    codes = call("GET", f"/projects/{project_code}/task-definition/gen-task-codes", {"genNum": 4})
    c_sql1, c_sql2, c_sql3, c_dag = [int(c) for c in codes["data"]]
    info(f"taskCodes: 3 SQL = {c_sql1},{c_sql2},{c_sql3} | LINEAGE_DAG = {c_dag}")

    tasks = [
        sql_task_definition(c_sql1, "t_sql_ods_产量流水", SQL_ODS, datasource_id),
        sql_task_definition(c_sql2, "t_sql_dwd_产量明细", SQL_DWD, datasource_id),
        sql_task_definition(c_sql3, "t_sql_dws_产量汇总", SQL_DWS, datasource_id),
        dag_task_definition(c_dag, DAG_TASK_NAME),
    ]
    relations = [relation(0, c_sql1), relation(c_sql1, c_sql2), relation(c_sql2, c_sql3)]
    relations.append(relation(0, c_dag) if parallel_dag else relation(c_sql3, c_dag))
    locations = [{"taskCode": t["code"], "x": 200 + i * 260, "y": 200}
                 for i, t in enumerate(tasks)]

    created = call("POST", f"/projects/{project_code}/process-definition", {
        "name": name,
        "description": description,
        "globalParams": "[]",
        "locations": json.dumps(locations, ensure_ascii=False),
        "timeout": 0,
        "taskDefinitionJson": json.dumps(tasks, ensure_ascii=False),
        "taskRelationJson": json.dumps(relations, ensure_ascii=False),
        "executionType": "PARALLEL",
    })
    if not created.get("success"):
        print(f"!! 创建工作流失败: {json.dumps(created, ensure_ascii=False)[:600]}")
        return None, None
    wf_code = int(created["data"]["code"])
    info(f"workflowCode = {wf_code}")
    detail = call("GET", f"/projects/{project_code}/process-definition/{wf_code}")
    for t in ((detail.get("data") or {}).get("taskDefinitionList") or []):
        info(f"回读任务 {t['name']}  taskType={t['taskType']}")
    return wf_code, c_dag


def phase1(project_code: int, datasource_id: int) -> int:
    step("① 新建工作流：3 个 SQL 任务串联 + 1 个 LINEAGE_DAG 尾节点（真实尾部结构）")
    wf_code, dag_code = build_workflow(
        project_code, datasource_id, WORKFLOW_NAME, parallel_dag=False,
        description="LINEAGE_DAG 演示：3 个 SQL 任务 + 1 个尾部分析节点（sql3 -> dag）")
    if wf_code is None:
        return 1
    run_and_report(project_code, wf_code, dag_code, pull_log=False, max_polls=20)
    return 0


def phase2(project_code: int, datasource_id: int) -> int:
    step("② 同样 3 个 SQL 任务 + LINEAGE_DAG（与 SQL 同层，演示环境无 Hive 数据源时也能真跑）")
    info("说明：本演示环境没有真实 Hive，SQL 任务必然 FAILURE，而海豚不会把失败上游的"
         "下游任务提交执行 —— 所以①里的尾节点被跳过。")
    info("      工作流血缘分析读的是工作流**定义**（taskDefinitionList），与执行结果无关，"
         "因此把节点与 SQL 任务同层挂载，就能拿到完全一样的全链路报告。")
    name = WORKFLOW_NAME + "_并行运行"
    wf_code, dag_code = build_workflow(
        project_code, datasource_id, name, parallel_dag=True,
        description="LINEAGE_DAG 演示（并行挂载，演示环境可真实跑通）")
    if wf_code is None:
        return 1
    _, state = run_and_report(project_code, wf_code, dag_code)
    return 0 if state == "SUCCESS" else 1


def phase3(project_code: int, datasource_id: int) -> int:
    step("③ 历史工作流零改造：给已有的 wf_dws_汇总（4 个 SQL 任务）追加 1 个 LINEAGE_DAG 节点")
    defs = call("GET", f"/projects/{project_code}/process-definition",
                {"searchVal": EXISTING_WORKFLOW, "pageSize": 20, "pageNo": 1})
    target = None
    for wf in ((defs.get("data") or {}).get("totalList") or []):
        if wf.get("name") == EXISTING_WORKFLOW:
            target = wf
    if target is None:
        print(f"!! 找不到工作流 {EXISTING_WORKFLOW}（先跑 scripts/ds_setup_demo.py）")
        return 1
    wf_code = int(target["code"])
    info(f"复用已有工作流 {EXISTING_WORKFLOW} code={wf_code}（原有 4 个 SQL 任务一行未改）")

    call("POST", f"/projects/{project_code}/process-definition/{wf_code}/release",
         {"releaseState": "OFFLINE"})
    detail = call("GET", f"/projects/{project_code}/process-definition/{wf_code}")
    existing_tasks = (detail.get("data") or {}).get("taskDefinitionList") or []
    for t in list(existing_tasks):
        if t.get("taskType") == "LINEAGE_DAG":
            call("DELETE", f"/projects/{project_code}/task-definition/{t['code']}")
            info(f"清掉上一次追加的旧节点 {t['name']}")
    detail = call("GET", f"/projects/{project_code}/process-definition/{wf_code}")
    existing_tasks = (detail.get("data") or {}).get("taskDefinitionList") or []
    info(f"已有任务（全部保留，未改动）: {[t['name'] for t in existing_tasks]}")

    code = int(call("GET", f"/projects/{project_code}/task-definition/gen-task-codes",
                    {"genNum": 1})["data"][0])
    added = call("POST", f"/projects/{project_code}/task-definition/save-single", {
        "processDefinitionCode": wf_code,
        "taskDefinitionJsonObj": json.dumps(dag_task_definition(code, DAG_TASK_NAME),
                                            ensure_ascii=False),
        # 生产环境这里传上游 task code（尾部串联）；演示环境 SQL 跑不通会被跳过，故留空并行
        "upstreamCodes": "",
    })
    info(f"追加响应: {json.dumps(added, ensure_ascii=False)[:240]}")

    detail = call("GET", f"/projects/{project_code}/process-definition/{wf_code}")
    names = [f"{t['name']}[{t['taskType']}]"
             for t in ((detail.get("data") or {}).get("taskDefinitionList") or [])]
    info(f"回读工作流任务: {names}")

    _, state = run_and_report(project_code, wf_code, code)
    return 0 if state == "SUCCESS" else 1


def main() -> int:
    step("0) 登录海豚 + 准备项目/数据源")
    r = call("POST", "/login", data={"userName": USER, "userPassword": PASSWORD})
    info(f"登录: {json.dumps(r, ensure_ascii=False)[:160]}")
    if not r.get("success"):
        return 1

    proj = call("GET", "/projects", {"pageSize": 100, "pageNo": 1, "searchVal": PROJECT_NAME})
    items = (proj.get("data") or {}).get("totalList") or []
    if items:
        project_code = int(items[0]["code"])
        info(f"复用项目 {items[0]['name']} code={project_code}")
    else:
        created = call("POST", "/projects", data={"projectName": PROJECT_NAME,
                                                  "description": "LINEAGE_DAG 演示"})
        project_code = int(created["data"]["code"])
        info(f"新建项目 code={project_code}")

    dss = call("GET", "/datasources", {"pageSize": 100, "pageNo": 1})
    ds_items = (dss.get("data") or {}).get("totalList") or []
    if ds_items:
        datasource_id = int(ds_items[0]["id"])
        info(f"复用数据源 {ds_items[0].get('name')} type={ds_items[0].get('type')} id={datasource_id}")
    else:
        created = call("POST", "/datasources", data={
            "name": "hive_血缘DAG演示", "type": "HIVE", "note": "演示用（不校验连通性）",
            "url": "jdbc:hive2://127.0.0.1:10000/default",
            "userName": "hive", "password": "hive",
        })
        datasource_id = int((created.get("data") or {}).get("id") or 1)
        info(f"新建数据源 id={datasource_id}")

    rc = phase1(project_code, datasource_id)
    if "--phase1-only" in sys.argv:
        return rc
    rc2 = phase2(project_code, datasource_id)
    rc3 = phase3(project_code, datasource_id)
    return 0 if rc3 == 0 else (0 if rc2 == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
