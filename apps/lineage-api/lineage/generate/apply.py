"""L3：一键落地 DolphinScheduler（链路 → 工作流定义 → 可选真实创建）。

安全第一
--------
* **默认不创建**（``create_workflow=false``）：只返回 ``workflow_json`` 供人工评审；
* 真要创建时：同名工作流**先 OFFLINE 再删除**（保证脚本可重复执行），再申请合法 task code
  并调用海豚 ``POST /projects/{pc}/process-definition``（走 form body，避免中文 SQL 撑爆 URL）；
* 创建后**回读**工作流定义，把任务清单 / 依赖关系原样返回，证明真的落库了。

任务链：每个 stage 一个 ``SQL(HIVE)`` 任务，``taskRelationJson`` 按链路的 ``depends_on`` 连边，
``locations`` 按执行顺序从左到右排布（链式依赖一眼可见）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from lineage.ds.client import DsApiError, DsAuthError, DsClient, DsConnectionError
from lineage.generate.spec import layer_cn

__all__ = ["build_workflow_json", "apply_pipeline", "format_apply_text"]

#: 离线（不连海豚）时给任务分配的假 task code 起点（海豚要求 19 位以内正整数）
OFFLINE_TASK_CODE_BASE = 91000000000000000
#: 任务在画布上的初始坐标与间距（链式依赖：从左往右一条线）
LOCATION_X0 = 240
LOCATION_DX = 280
LOCATION_Y0 = 200


def _task_name(index: int, stage: Dict[str, Any]) -> str:
    """任务名：``t1_ads_产销存月报``（序号 + 分层 + 短表名）。"""
    table = str(stage.get("target_table") or "")
    short = table.split(".")[-1]
    layer = str(stage.get("layer") or "")
    return f"t{index}_{layer}_{short}" if layer else f"t{index}_{short}"


def build_workflow_json(stages: Sequence[Dict[str, Any]], *, project_code: Any = 0,
                        workflow_name: str = "wf_gen_pipeline", env: str = "hive",
                        datasource_id: int = 1, task_codes: Optional[Sequence[int]] = None,
                        description: str = "", worker_group: str = "default",
                        tenant: str = "default") -> Dict[str, Any]:
    """把 L2 的 stages 转成海豚工作流定义（taskDefinitionJson / taskRelationJson / locations）。

    ``stages`` 按「目标层在前」的顺序传入（L2 的输出顺序），这里会反过来让**上游先执行**。
    """
    ordered: List[Dict[str, Any]] = [s for s in stages if (s.get("sql") or "").strip()]
    ordered = list(reversed(ordered))
    if not ordered:
        return {"ok": False, "error": "没有可落地的 stage（stages 为空或每段 SQL 都为空）",
                "tasks": [], "workflow": {}}

    codes: List[int] = list(task_codes or [])
    while len(codes) < len(ordered):
        codes.append(OFFLINE_TASK_CODE_BASE + len(codes) + 1)

    producer: Dict[str, int] = {}
    task_definitions: List[Dict[str, Any]] = []
    stage_meta: List[Dict[str, Any]] = []
    for idx, stage in enumerate(ordered):
        table = str(stage.get("target_table") or "")
        if table:
            producer[table] = codes[idx]
    for idx, stage in enumerate(ordered):
        table = str(stage.get("target_table") or "")
        layer = stage.get("layer") or ""
        sql = str(stage.get("sql") or "")
        deps = [d for d in (stage.get("depends_on") or []) if d in producer and producer[d] != codes[idx]]
        pre = [
            {
                "name": "",
                "preTaskCode": int(producer[d]),
                "preTaskVersion": 1,
                "postTaskCode": int(codes[idx]),
                "postTaskVersion": 1,
                "conditionType": "NONE",
                "conditionParams": {},
            }
            for d in deps
        ]
        if not pre:
            pre = [{
                "name": "",
                "preTaskCode": 0,
                "preTaskVersion": 0,
                "postTaskCode": int(codes[idx]),
                "postTaskVersion": 1,
                "conditionType": "NONE",
                "conditionParams": {},
            }]
        task_definitions.append({
            "code": int(codes[idx]),
            "name": _task_name(idx + 1, stage),
            "version": 1,
            "description": (f"{layer_cn(layer)}：{table}"
                            + (f"（链路外依赖 {'、'.join(stage['external_inputs'])}）"
                               if stage.get("external_inputs") else "")),
            "delayTime": 0,
            "taskType": "SQL",
            "taskParams": {
                "localParams": [], "resourceList": [],
                "type": str(env or "hive").upper(),
                "datasource": int(datasource_id),
                "sql": sql,
                "sqlType": "1",              # 0=查询 1=非查询（INSERT 属非查询）
                "preStatements": [], "postStatements": [],
                "displayRows": 10,
            },
            "flag": "YES",
            "isCache": "NO",
            "taskPriority": "MEDIUM",
            "workerGroup": worker_group,
            "environmentCode": -1,
            "failRetryTimes": 0,
            "failRetryInterval": 1,
            "timeoutFlag": "CLOSE",
            "timeoutNotifyStrategy": "",
            "timeout": 0,
            "taskExecuteType": "BATCH",
        })
        stage_meta.append({"target_table": table, "layer": layer,
                           "depends_on": deps, "external_inputs": list(stage.get("external_inputs") or []),
                           "task_name": task_definitions[-1]["name"],
                           "task_code": int(codes[idx]), "relations": pre})

    relations: List[Dict[str, Any]] = []
    for meta in stage_meta:
        relations.extend(meta["relations"])
    locations = [
        {"taskCode": int(codes[idx]), "x": LOCATION_X0 + idx * LOCATION_DX, "y": LOCATION_Y0}
        for idx in range(len(ordered))
    ]
    workflow = {
        "name": workflow_name,
        "description": description or f"由 sql-lineage-mvp generate 生成的分层链路（{len(ordered)} 个节点）",
        "projectCode": int(project_code or 0),
        "globalParams": "[]",
        "timeout": 0,
        "executionType": "PARALLEL",
        "locations": json.dumps(locations, ensure_ascii=False),
        "taskDefinitionJson": json.dumps(task_definitions, ensure_ascii=False),
        "taskRelationJson": json.dumps(relations, ensure_ascii=False),
        "taskRelationJsonObject": relations,
    }
    return {"ok": True, "workflow": workflow, "tasks": task_definitions,
            "task_codes": [int(c) for c in codes[:len(ordered)]],
            "locations": locations, "stage_meta": stage_meta,
            "execution_order": [t["name"] for t in task_definitions]}


def _deploy_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "base_url": payload.get("base_url"),
        "user": payload.get("user"),
        "password": payload.get("password"),
        "timeout": float(payload.get("timeout") or 30.0),
    }


def _ensure_project(client: DsClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """定位落地项目：project_code > project_name > 第一个项目。"""
    code = payload.get("project_code")
    name = str(payload.get("project_name") or "").strip()
    projects = client.list_projects()
    if code:
        for p in projects:
            if str(p.get("code")) == str(code):
                return p
        return {"code": int(code), "name": name or f"code={code}"}
    if name:
        for p in projects:
            if p.get("name") == name:
                return p
    if projects:
        return projects[0]
    raise DsApiError(-1, "海豚里没有任何项目：请先在 DolphinScheduler 建一个项目，或传 project_code")


def _ensure_datasource(client: DsClient, payload: Dict[str, Any]) -> int:
    """SQL 任务要绑数据源 id：优先用显式传入的，其次复用 HIVE 数据源，最后建一个演示用。"""
    explicit = payload.get("datasource_id")
    if explicit:
        return int(explicit)
    wanted = str(payload.get("env") or "hive").upper()
    for ds in client.list_datasources():
        if str(ds.get("type") or "").upper() == wanted and int(ds.get("id") or 0) > 0:
            return int(ds["id"])
    for ds in client.list_datasources():
        if int(ds.get("id") or 0) > 0:
            return int(ds["id"])
    created = client.create_datasource({
        "name": f"gen_{wanted.lower()}_auto",
        "note": "sql-lineage-mvp generate 自动创建的演示数据源（不校验连通性，只给 SQL 任务挂 id）",
        "type": wanted,
        "url": "jdbc:hive2://localhost:10000/default",
        "username": "hive",
        "password": "",
    })
    return int((created or {}).get("id") or 1)


def apply_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /generate/apply`` 与 ``generate apply``：链路 → 海豚工作流（可选真实创建）。"""
    started = time.perf_counter()
    pipeline = payload.get("pipeline") or {}
    stages: List[Dict[str, Any]] = list(payload.get("stages") or pipeline.get("stages") or [])
    if not stages:
        return {"success": False, "mode": "L3",
                "error": "缺少 stages：请把 generate pipeline 的返回整体放进 pipeline 字段，"
                         "或直接传 stages 数组"}
    workflow_name = str(payload.get("workflow_name") or "").strip()
    if not workflow_name:
        requirement = str(pipeline.get("requirement") or payload.get("requirement") or "分层链路")
        workflow_name = "wf_gen_" + requirement.replace(" ", "")
    create = payload.get("create_workflow") in (True, "true", "True", 1, "1", "on")
    env = str(payload.get("env") or "hive")
    project_code = payload.get("project_code") or 0
    datasource_id = int(payload.get("datasource_id") or 1)

    notes: List[str] = []
    codes: Optional[List[int]] = None
    client: Optional[DsClient] = None
    project: Dict[str, Any] = {"code": int(project_code or 0), "name": ""}
    if create:
        try:
            client = DsClient(**_deploy_context(payload))
            client.ensure_login()
            project = _ensure_project(client, payload)
            project_code = int(project.get("code") or project_code or 0)
            datasource_id = _ensure_datasource(client, payload)
            codes = client.gen_task_codes(project_code, len(stages))
        except (DsConnectionError, DsAuthError, DsApiError) as exc:
            if client is not None:
                client.close()
            return {"success": False, "mode": "L3", "error": f"{type(exc).__name__}: {exc}",
                    "hint": "海豚不可用时可以先只生成 JSON（create_workflow=false）；"
                            "容器需在跑：docker ps | grep dolphinscheduler"}

    built = build_workflow_json(stages, project_code=project_code, workflow_name=workflow_name,
                                env=env, datasource_id=datasource_id, task_codes=codes,
                                description=str(payload.get("description") or ""),
                                worker_group=str(payload.get("worker_group") or "default"))
    if not built.get("ok"):
        if client is not None:
            client.close()
        return {"success": False, "mode": "L3", "error": built.get("error")}

    result: Dict[str, Any] = {
        "success": True, "mode": "L3",
        "workflow_name": workflow_name,
        "env": env,
        "project_code": project_code,
        "datasource_id": datasource_id,
        "task_count": len(built["tasks"]),
        "task_codes": built["task_codes"],
        "execution_order": built["execution_order"],
        "workflow_json": built["workflow"],
        "locations": built["locations"],
        "created": False,
        "notes": notes,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }

    if not create:
        notes.append("本次只生成工作流 JSON（create_workflow=false，安全默认）："
                     "请人工评审 SQL / 依赖关系后，再加 create_workflow=true 一键落地")
        notes.append("离线模式下 task code 为占位值（9.1e16 起），真实创建时会向海豚申请合法 code")
        result["hint"] = ("CLI: python -m lineage.cli generate apply --from-pipeline pipeline.json "
                          "--workflow-name %s --project-code <pc> --apply --json" % workflow_name)
        return result

    assert client is not None
    try:
        # 同名工作流先 OFFLINE 再删除，保证可重复执行
        removed: List[Dict[str, Any]] = []
        for wf in client.list_process_definitions(project_code, search_val=workflow_name):
            if str(wf.get("name")) != workflow_name:
                continue
            client.request("POST", f"/projects/{int(project_code)}/process-definition/"
                                   f"{int(wf['code'])}/release", params={"releaseState": "OFFLINE"},
                           raise_on_error=False)
            client.delete_process_definition(project_code, wf["code"])
            removed.append({"code": wf["code"], "name": wf.get("name")})
        if removed:
            notes.append(f"已删除同名旧工作流 {len(removed)} 个（先 OFFLINE 再删除）："
                         + "、".join(str(r["code"]) for r in removed))

        created = client.create_process_definition(
            project_code, workflow_name,
            task_definitions=built["tasks"],
            task_relations=built["workflow"]["taskRelationJsonObject"],
            description=built["workflow"]["description"],
            locations=built["workflow"]["locations"],
        )
        workflow_code = int((created or {}).get("code") or 0)
        result["created"] = True
        result["workflow_code"] = workflow_code
        result["create_response"] = created
        result["project"] = {"code": project_code, "name": project.get("name") or ""}

        detail = client.get_process_definition(project_code, workflow_code)
        readback = [{"name": t.get("name"), "taskType": t.get("taskType"),
                     "datasource": (t.get("taskParams") or {}).get("datasource")
                     if isinstance(t.get("taskParams"), dict) else None,
                     "sql_len": len(str((t.get("taskParams") or {}).get("sql") or ""))
                     if isinstance(t.get("taskParams"), dict) else 0,
                     "pre": sorted({int(r.get("preTaskCode") or 0)
                                    for r in detail["processTaskRelationList"]
                                    if int(r.get("postTaskCode") or 0) == int(t.get("code") or 0)})}
                    for t in detail["taskDefinitionList"]]
        result["readback"] = {
            "workflow": detail["processDefinition"],
            "task_count": len(detail["taskDefinitionList"]),
            "relation_count": len(detail["processTaskRelationList"]),
            "tasks": readback,
        }
        notes.append(f"已真实创建并回读校验：workflow_code={workflow_code}，"
                     f"{len(detail['taskDefinitionList'])} 个任务 / "
                     f"{len(detail['processTaskRelationList'])} 条依赖")
        result["hint"] = (f"可在海豚界面查看：项目 {project.get('name') or project_code} → "
                          f"工作流 {workflow_name}（code={workflow_code}）")
    except (DsApiError, DsConnectionError, DsAuthError) as exc:
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["hint"] = "工作流 JSON 已在上面的 workflow_json 里，可人工到海豚界面导入"
    finally:
        client.close()
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def format_apply_text(result: Dict[str, Any]) -> str:
    if not result.get("success"):
        return ("落地失败：" + str(result.get("error") or "未知错误")
                + (f"\n提示：{result.get('hint')}" if result.get("hint") else ""))
    lines = ["=" * 72,
             f"L3 调度落地：{result['workflow_name']}（项目 {result.get('project_code')}，"
             f"{result.get('task_count')} 个任务，env={result.get('env')}）",
             "=" * 72]
    lines.append("执行顺序（上游 -> 下游）：")
    for idx, name in enumerate(result.get("execution_order") or [], start=1):
        lines.append(f"  {idx}. {name}  (taskCode={result['task_codes'][idx - 1]})")
    lines.append("-" * 72)
    lines.append("工作流 JSON（可直接人工评审 / 粘贴到海豚接口）：")
    wf = result.get("workflow_json") or {}
    for key in ("name", "projectCode", "executionType", "locations"):
        lines.append(f"  {key}: {wf.get(key)}")
    lines.append(f"  taskDefinitionJson: [{len(json.loads(wf.get('taskDefinitionJson') or '[]'))} 个任务定义]")
    lines.append(f"  taskRelationJson: [{len(json.loads(wf.get('taskRelationJson') or '[]'))} 条依赖]")
    lines.append("-" * 72)
    lines.append(f"是否真实创建：{'✅ 是' if result.get('created') else '❌ 否（create_workflow=false，安全默认）'}")
    if result.get("created"):
        lines.append(f"workflow_code = {result.get('workflow_code')}；"
                     f"回读任务 {result['readback']['task_count']} 个 / "
                     f"依赖 {result['readback']['relation_count']} 条")
        for task in result["readback"]["tasks"]:
            lines.append(f"    - {task['name']}  taskType={task['taskType']} "
                         f"sql={task['sql_len']} 字符  preTaskCode={task['pre']}")
    for note in result.get("notes") or []:
        lines.append(f"  · {note}")
    if result.get("hint"):
        lines.append("提示：" + str(result["hint"]))
    lines.append("=" * 72)
    return "\n".join(lines)
