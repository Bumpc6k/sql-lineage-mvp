#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在本地 DolphinScheduler 上创建「SQL + SHELL + PYTHON」三任务工作流，用于实测脚本解析。

做三件事：
1. 复用（或新建）演示项目，删掉同名旧工作流，保证可重复执行；
2. 建工作流 ``wf_脚本解析实测``，三个任务串成一条链：
   ``t_dwd_产量明细``（SQL）→ ``t_库存_脚本``（SHELL，内含 hive -e / 变量 / heredoc）
   → ``t_产销存_pyspark``（PYTHON，内含 spark.sql 变量 / f-string / read_sql）；
3. 直接调 ``lineage.workflow.analyze_workflow`` 跑一遍工作流级分析，打印每个任务
   的 ``script_kind`` / ``sql_count`` / 输入输出表，并把结果落盘到 reports/。

用法::

    .venv/bin/python demos/ds_add_script_workflow.py
    .venv/bin/python demos/ds_add_script_workflow.py --dry-run     # 只打印脚本，不建工作流
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lineage.ds.client import DsClient, DsError  # noqa: E402
from lineage.ds.workflow import analyze_workflow  # noqa: E402

DEMO_PROJECT = "烟草数仓演示"
WORKFLOW_NAME = "wf_脚本解析实测"
OUT_JSON = PROJECT_ROOT / "reports" / "script_workflow_probe.json"

# --------------------------------------------------------------------------- #
# 三个任务的脚本
# --------------------------------------------------------------------------- #
SQL_DWD = """-- DWD 明细：ODS 产量流水 + 维表 -> cdw.dwd_卷烟产量明细
INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '2026-01-01')
SELECT
    p.work_order_no AS work_order_no,
    p.plant_code    AS plant_code,
    p.output_qty    AS output_qty,
    pl.plant_name   AS plant_name
FROM ods.ods_卷烟产量流水 p
LEFT JOIN dim.dim_plant pl ON p.plant_code = pl.plant_code
WHERE p.dt = '2026-01-01';
"""

SHELL_SCRIPT = r"""#!/bin/bash
# SHELL 任务：hive -e 多语句 + shell 变量 SQL + heredoc + 外部 SQL 文件引用
set -euo pipefail
DT='2026-01-01'
echo "[INFO] 开始 DWS 层加工 dt=${DT}"

hive -e "
INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')
SELECT t.plant_code AS plant_code, t.biz_type AS biz_type, SUM(t.qty) AS qty
FROM (
    SELECT plant_code, '产量' AS biz_type, output_qty AS qty FROM ods.ods_卷烟产量流水 WHERE dt = '2026-01-01'
    UNION ALL
    SELECT plant_code, '销量' AS biz_type, sale_qty AS qty FROM ods.ods_卷烟销量流水 WHERE dt = '2026-01-01'
) t
GROUP BY t.plant_code, t.biz_type;
"

SQL_STOCK="INSERT OVERWRITE TABLE cdw.dws_库存基线 PARTITION (dt = '2026-01-01')
SELECT c.plant_code AS plant_code, SUM(c.stock_qty) AS stock_qty
FROM ods.ods_成品库存快照 c WHERE c.dt = '2026-01-01'
GROUP BY c.plant_code;"
beeline -e "$SQL_STOCK"

hive <<EOF
INSERT OVERWRITE TABLE cdw.dws_税利汇总 PARTITION (dt = '2026-01-01')
SELECT t.plant_code AS plant_code, SUM(t.tax_amt) AS tax_amt
FROM ods.ods_税利上缴流水 t WHERE t.dt = '2026-01-01'
GROUP BY t.plant_code;
EOF

beeline -f etl/dws_设备效率汇总.sql
echo "[INFO] DWS 层加工结束"
"""

PYTHON_SCRIPT = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PYTHON(PySpark) 任务：ADS 层指标加工。"""
from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("ads_经营指标").enableHiveSupport().getOrCreate()
DT = "2026-01-01"

SQL_ADS = """
INSERT OVERWRITE TABLE ads.ads_经营指标明细 PARTITION (dt = '2026-01-01')
SELECT d.plant_code AS plant_code, d.qty AS qty, p.plant_name AS plant_name
FROM cdw.dws_产销存汇总 d
LEFT JOIN dim.dim_plant p ON d.plant_code = p.plant_code
WHERE d.dt = '2026-01-01'
"""
spark.sql(SQL_ADS)

spark.sql(f"""
INSERT OVERWRITE TABLE ads.ads_库存日报 PARTITION (dt = '{DT}')
SELECT b.plant_code AS plant_code, b.stock_qty AS stock_qty
FROM cdw.dws_库存基线 b
WHERE b.dt = '{DT}'
""")
'''


# --------------------------------------------------------------------------- #
def _task(code: int, name: str, ttype: str, params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": code, "name": name, "version": 1,
        "description": f"{name} 节点（{ttype}）", "delayTime": 0, "taskType": ttype,
        "taskParams": params, "flag": "YES", "isCache": "NO", "taskPriority": "MEDIUM",
        "workerGroup": "default", "environmentCode": -1, "failRetryTimes": 0,
        "failRetryInterval": 1, "timeoutFlag": "CLOSE", "timeoutNotifyStrategy": "",
        "timeout": 0, "taskExecuteType": "BATCH",
    }


def _relation(pre: int, post: int) -> Dict[str, Any]:
    return {"name": "", "preTaskCode": pre, "postTaskCode": post,
            "preTaskVersion": 0 if pre == 0 else 1, "postTaskVersion": 1,
            "conditionType": "NONE", "conditionParams": {}}


def _find_project(client: DsClient) -> Dict[str, Any]:
    for proj in client.list_projects():
        if proj.get("name") == DEMO_PROJECT:
            return proj
    return client.create_project(DEMO_PROJECT, "脚本解析实测（SQL + SHELL + PYTHON）")


def _datasource_id(client: DsClient) -> int:
    for ds in client.list_datasources():
        if str(ds.get("type") or "").upper() == "HIVE":
            return int(ds["id"])
    created = client.create_datasource({
        "name": "hive_脚本解析实测", "note": "脚本解析实测演示", "type": "HIVE",
        "host": "localhost", "port": 10000, "database": "default",
        "userName": "hive", "password": "hive", "other": {"hive.server2.auth": "NONE"},
    })
    return int(created["id"])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="创建 SQL+SHELL+PYTHON 演示工作流并实测脚本解析")
    ap.add_argument("--base-url", default="http://localhost:12345/dolphinscheduler")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="dolphinscheduler123")
    ap.add_argument("--dry-run", action="store_true", help="只打印脚本，不建工作流")
    args = ap.parse_args(argv)

    if args.dry_run:
        print("=== SQL 任务 ==="); print(SQL_DWD)
        print("=== SHELL 任务 ==="); print(SHELL_SCRIPT)
        print("=== PYTHON 任务 ==="); print(PYTHON_SCRIPT)
        return 0

    client = DsClient(base_url=args.base_url, user=args.user, password=args.password)
    try:
        client.ensure_login()
        project = _find_project(client)
        project_code = int(project["code"])
        datasource_id = _datasource_id(client)

        for definition in client.list_process_definitions(project_code):
            if definition.get("name") == WORKFLOW_NAME:
                client.delete_process_definition(project_code, definition["code"])
                print(f"[清理] 删除旧工作流 {WORKFLOW_NAME}（code={definition['code']}）")

        codes = client.gen_task_codes(project_code, 3)
        c_sql, c_sh, c_py = codes[0], codes[1], codes[2]
        task_defs = [
            _task(c_sql, "t_dwd_产量明细", "SQL",
                  {"localParams": [], "resourceList": [], "type": "HIVE",
                   "datasource": datasource_id, "sql": SQL_DWD, "sqlType": "1",
                   "preStatements": [], "postStatements": [], "displayRows": 10}),
            _task(c_sh, "t_dws_脚本加工", "SHELL",
                  {"localParams": [], "resourceList": [], "rawScript": SHELL_SCRIPT}),
            _task(c_py, "t_ads_pyspark", "PYTHON",
                  {"localParams": [], "resourceList": [], "rawScript": PYTHON_SCRIPT}),
        ]
        relations = [_relation(0, c_sql), _relation(c_sql, c_sh), _relation(c_sh, c_py)]
        created = client.create_process_definition(
            project_code=project_code, name=WORKFLOW_NAME,
            description="脚本解析实测：SQL + SHELL(hive -e/变量/heredoc) + PYTHON(spark.sql)",
            task_definitions=task_defs, task_relations=relations,
        )
        definition_code = int(created["code"])
        print(f"[工作流] 已创建 {WORKFLOW_NAME}（project={project_code}, code={definition_code}）")
    except DsError as e:
        print(f"错误：海豚调用失败 -> {e}", file=sys.stderr)
        return 2
    finally:
        client.close()

    # ---- 直接跑工作流级分析（LINEAGE_DAG 服务端实现），打印真实结果 ---- #
    result = analyze_workflow({
        "ds_base": args.base_url, "ds_user": args.user, "ds_password": args.password,
        "project_code": project_code, "process_define_code": definition_code,
        "task_types": ["SQL", "SHELL", "PYTHON"], "with_report": False,
    })
    if not result.get("success"):
        print(f"错误：工作流分析失败 -> {result.get('error')}", file=sys.stderr)
        return 1

    wf = result["workflow"]
    print("=" * 78)
    print(f"工作流 {wf['name']}（code={wf['code']}）  任务 {wf['task_count']} 个 / "
          f"解析成功 {wf['parsed_task_count']} 个  语句 {wf['statement_count']} 条  "
          f"SQL 面 {wf.get('sql_count')} 条  脚本类型分布 {wf.get('script_kinds')}")
    for t in result["tasks"]:
        print("-" * 78)
        print(f"[{t['name']}] type={t['type']} script_kind={t['script_kind']} "
              f"sql_count={t['sql_count']} 语句={t['statement_count']} 字段={t['column_count']}")
        print(f"  输入表: {', '.join(t['input_tables']) or '(无)'}")
        print(f"  输出表: {', '.join(t['output_tables']) or '(无)'}")
        for h in t["unresolved_hints"]:
            print(f"  ! 未解析: {h}")
        for e in t["errors"]:
            print(f"  ! 错误: {e}")
    print("-" * 78)
    print(f"表级血缘 {len(result['merged']['table_lineage'])} 条 / "
          f"字段级血缘 {result['merged']['column_lineage_count']} 条")
    print(f"工作流内链路: {' -> '.join(result['chain_in_workflow'])}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整结果已写入 {OUT_JSON.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
