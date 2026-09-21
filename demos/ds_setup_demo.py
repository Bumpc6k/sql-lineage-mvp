#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在本地 DolphinScheduler 上创建 P3 演示数据（可重复执行）。

做三件事：
1. 通过海豚 OpenAPI 建一个项目「烟草数仓演示」（已存在则先删后建，保证可重跑）；
2. 建 4 个有依赖关系的工作流（ODS 采集 -> DWD 清洗 -> DWS 汇总 -> ADS 报表），
   共 16 个任务节点，任务里放真实形态的数仓 SQL（SQL 任务放 taskParams.sql，
   SHELL 任务放 taskParams.rawScript，另有 1 个 DEPENDENT 任务声明海豚原生工作流依赖）；
3. 把海豚上的工作流定义（含任务、任务关系）原样导出到 docs/ds_demo_workflows/，
   并把每个任务的脚本单独存成 .sql 便于人工核对。

用法::

    .venv/bin/python demos/ds_setup_demo.py                    # 全量重建 + 导出
    .venv/bin/python demos/ds_setup_demo.py --no-export        # 只建数据
    .venv/bin/python demos/ds_setup_demo.py --base-url http://localhost:12345/dolphinscheduler

环境变量：DS_BASE_URL / DS_USER / DS_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lineage.ds.client import (  # noqa: E402
    DsApiError,
    DsAuthError,
    DsClient,
    DsConnectionError,
    DsError,
    extract_scripts,
)

DEMO_PROJECT = "烟草数仓演示"
DEMO_PROJECT_DESC = "P3 血缘演示项目：ODS->DWD->DWS->ADS 四层调度链路（旁路集成，不改海豚源码）"
DEMO_DATASOURCE = "hive_演示"

# --------------------------------------------------------------------------- #
# 演示工作流定义（任务里的 SQL 与 examples/warehouse/ 的场景呼应）
# --------------------------------------------------------------------------- #
ODS_产量 = """-- ODS 贴源：ERP 生产工单 -> ods.ods_卷烟产量流水
INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')
SELECT
    a.work_order_no                        AS work_order_no,
    a.plant_code                           AS plant_code,
    a.brand_code                           AS brand_code,
    a.work_date                            AS work_date,
    CAST(a.output_qty AS DECIMAL(18, 4))   AS output_qty,
    CAST(a.defect_qty AS DECIMAL(18, 4))   AS defect_qty,
    a.shift_code                           AS shift_code,
    a.update_time                          AS update_time
FROM src.erp_生产工单明细 a
WHERE a.dt = '2026-01-01'
  AND a.output_qty IS NOT NULL;
"""

ODS_销量 = """-- ODS 贴源：MES 销售出库 -> ods.ods_卷烟销量流水
INSERT OVERWRITE TABLE ods.ods_卷烟销量流水 PARTITION (dt = '2026-01-01')
SELECT
    b.outbound_no                          AS outbound_no,
    b.customer_code                        AS customer_code,
    b.brand_code                           AS brand_code,
    b.sale_date                            AS sale_date,
    CAST(b.sale_qty AS DECIMAL(18, 4))     AS sale_qty,
    CAST(b.sale_amt AS DECIMAL(18, 2))     AS sale_amt,
    b.channel_code                         AS channel_code,
    b.update_time                          AS update_time
FROM src.mes_销售出库明细 b
WHERE b.dt = '2026-01-01'
  AND b.sale_qty > 0;
"""

ODS_税利 = """-- ODS 贴源：财务税金凭证 -> ods.ods_税利上缴流水
INSERT OVERWRITE TABLE ods.ods_税利上缴流水 PARTITION (dt = '2026-01-01')
SELECT
    d.voucher_no                           AS voucher_no,
    d.plant_code                           AS plant_code,
    d.brand_code                           AS brand_code,
    d.tax_type                             AS tax_type,
    d.stat_month                           AS stat_month,
    CAST(d.tax_amt AS DECIMAL(18, 2))      AS tax_amt,
    CAST(d.profit_amt AS DECIMAL(18, 2))   AS profit_amt,
    d.update_time                          AS update_time
FROM src.fin_税金凭证 d
WHERE d.dt = '2026-01-01'
  AND d.stat_month >= '2026-01'
  AND d.plant_code IS NOT NULL;
"""

# SHELL 任务：rawScript 里是 hive -e 包着的多语句 SQL（顺带验证 SHELL 脚本抽取）
ODS_库存_SHELL = """# 贴源：WMS 库存快照 -> ods.ods_成品库存快照（hive -e 多语句，SHELL 任务形态）
hive -e "
INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '2026-01-01')
SELECT
    c.warehouse_code                       AS warehouse_code,
    c.plant_code                           AS plant_code,
    c.brand_code                           AS brand_code,
    c.stock_date                           AS stock_date,
    CAST(c.stock_qty AS DECIMAL(18, 4))    AS stock_qty,
    CAST(c.stock_amt AS DECIMAL(18, 2))    AS stock_amt,
    c.batch_no                             AS batch_no,
    c.update_time                          AS update_time
FROM src.wms_库存快照 c
WHERE c.dt = '2026-01-01'
  AND c.warehouse_code IS NOT NULL;
INSERT OVERWRITE TABLE ods.ods_库存快照基线 PARTITION (dt = '2026-01-01')
SELECT
    c.plant_code                           AS plant_code,
    c.brand_code                           AS brand_code,
    SUM(CAST(c.stock_qty AS DECIMAL(18, 4))) AS base_stock_qty
FROM src.wms_库存快照 c
WHERE c.dt = '2026-01-01'
  AND c.batch_no IS NOT NULL
GROUP BY c.plant_code, c.brand_code;
"
"""

DWD_产量 = """-- DWD 明细：产量贴源 + 工厂 / 牌号维表 -> cdw.dwd_卷烟产量明细
INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '2026-01-01')
SELECT
    p.work_order_no                        AS work_order_no,
    p.work_date                            AS work_date,
    p.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    p.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    bd.price_band                          AS price_band,
    p.output_qty                           AS output_qty,
    p.output_qty * 250                     AS output_qty_cig,
    p.defect_qty                           AS defect_qty,
    CASE WHEN p.output_qty > 0
         THEN ROUND(p.defect_qty / p.output_qty, 6)
         ELSE 0 END                        AS defect_rate,
    p.shift_code                           AS shift_code
FROM ods.ods_卷烟产量流水 p
LEFT JOIN dim.dim_plant pl
       ON p.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON p.brand_code = bd.brand_code
WHERE p.dt = '2026-01-01';
"""

DWD_销量 = """-- DWD 明细：销量贴源 + 牌号维表 -> cdw.dwd_卷烟销量明细
INSERT OVERWRITE TABLE cdw.dwd_卷烟销量明细 PARTITION (dt = '2026-01-01')
SELECT
    s.outbound_no                          AS outbound_no,
    s.sale_date                            AS sale_date,
    s.customer_code                        AS customer_code,
    s.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    s.channel_code                         AS channel_code,
    s.sale_qty                             AS sale_qty,
    s.sale_amt                             AS sale_amt,
    CASE WHEN s.sale_qty > 0
         THEN ROUND(s.sale_amt / s.sale_qty, 2)
         ELSE 0 END                        AS unit_price
FROM ods.ods_卷烟销量流水 s
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
"""

DWD_库存 = """-- DWD 明细：库存快照 + 工厂维表 -> cdw.dwd_成品库存明细
INSERT OVERWRITE TABLE cdw.dwd_成品库存明细 PARTITION (dt = '2026-01-01')
SELECT
    k.warehouse_code                       AS warehouse_code,
    k.stock_date                           AS stock_date,
    k.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    k.brand_code                           AS brand_code,
    k.batch_no                             AS batch_no,
    k.stock_qty                            AS stock_qty,
    k.stock_amt                            AS stock_amt,
    ROUND(k.stock_amt / NULLIF(k.stock_qty, 0), 2) AS stock_unit_cost
FROM ods.ods_成品库存快照 k
LEFT JOIN dim.dim_plant pl
       ON k.plant_code = pl.plant_code
WHERE k.dt = '2026-01-01';
"""

DWD_税利 = """-- DWD 明细：税利贴源 + 牌号维表 -> cdw.dwd_税利明细
INSERT OVERWRITE TABLE cdw.dwd_税利明细 PARTITION (dt = '2026-01-01')
SELECT
    t.voucher_no                           AS voucher_no,
    t.stat_month                           AS stat_month,
    t.plant_code                           AS plant_code,
    t.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    t.tax_type                             AS tax_type,
    t.tax_amt                              AS tax_amt,
    t.profit_amt                           AS profit_amt,
    t.tax_amt + t.profit_amt               AS total_tax_profit
FROM ods.ods_税利上缴流水 t
LEFT JOIN dim.dim_brand bd
       ON t.brand_code = bd.brand_code
WHERE t.dt = '2026-01-01';
"""

DWS_产量汇总 = """-- DWS 汇总：按厂 / 牌号聚合产量
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                           AS plant_code,
    d.brand_code                           AS brand_code,
    SUM(d.output_qty)                      AS total_output_qty,
    SUM(d.defect_qty)                      AS total_defect_qty,
    COUNT(1)                               AS work_order_cnt
FROM cdw.dwd_卷烟产量明细 d
WHERE d.dt = '2026-01-01'
GROUP BY d.plant_code, d.brand_code;
"""

DWS_库存汇总 = """-- DWS 汇总：按厂 / 牌号聚合库存
INSERT OVERWRITE TABLE cdw.dws_库存汇总 PARTITION (dt = '2026-01-01')
SELECT
    k.plant_code                           AS plant_code,
    k.brand_code                           AS brand_code,
    SUM(k.stock_qty)                       AS total_stock_qty,
    SUM(k.stock_amt)                       AS total_stock_amt,
    COUNT(DISTINCT k.batch_no)             AS batch_cnt
FROM cdw.dwd_成品库存明细 k
WHERE k.dt = '2026-01-01'
GROUP BY k.plant_code, k.brand_code;
"""

DWS_产销存汇总 = """-- DWS 汇总：产量汇总 + 库存汇总 + 销量明细 -> 产销存汇总
INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')
SELECT
    a.plant_code                           AS plant_code,
    a.brand_code                           AS brand_code,
    a.total_output_qty                     AS output_qty,
    COALESCE(s.total_sale_qty, 0)          AS sale_qty,
    COALESCE(k.total_stock_qty, 0)         AS stock_qty,
    COALESCE(s.total_sale_amt, 0)          AS sale_amt
FROM cdw.dws_产量汇总 a
LEFT JOIN (
    SELECT
        d.plant_code                       AS plant_code,
        d.brand_code                       AS brand_code,
        SUM(d.sale_qty)                    AS total_sale_qty,
        SUM(d.sale_amt)                    AS total_sale_amt
    FROM cdw.dwd_卷烟销量明细 d
    WHERE d.dt = '2026-01-01'
    GROUP BY d.plant_code, d.brand_code
) s
  ON a.plant_code = s.plant_code AND a.brand_code = s.brand_code
LEFT JOIN cdw.dws_库存汇总 k
  ON a.plant_code = k.plant_code AND a.brand_code = k.brand_code
WHERE a.dt = '2026-01-01';
"""

DWS_税利汇总 = """-- DWS 汇总：按厂 / 牌号 / 月份聚合税利，并算单箱税利
INSERT OVERWRITE TABLE cdw.dws_税利汇总 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                           AS plant_code,
    t.brand_code                           AS brand_code,
    t.stat_month                           AS stat_month,
    SUM(t.tax_amt)                         AS total_tax_amt,
    SUM(t.profit_amt)                      AS total_profit_amt,
    SUM(t.total_tax_profit)                AS total_tax_profit,
    MAX(p.total_output_qty)                AS total_output_qty,
    CASE WHEN MAX(p.total_output_qty) > 0
         THEN ROUND(SUM(t.total_tax_profit) / MAX(p.total_output_qty), 2)
         ELSE 0 END                        AS tax_profit_per_box
FROM cdw.dwd_税利明细 t
LEFT JOIN cdw.dws_产量汇总 p
       ON t.plant_code = p.plant_code AND t.brand_code = p.brand_code
WHERE t.dt = '2026-01-01'
GROUP BY t.plant_code, t.brand_code, t.stat_month;
"""

ADS_产销存月报 = """-- ADS 报表：产销存月报（产销存汇总 + 工厂 / 牌号维表）
INSERT OVERWRITE TABLE ads.ads_产销存月报 PARTITION (dt = '2026-01-01')
SELECT
    s.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    s.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    bd.price_band                          AS price_band,
    s.output_qty                           AS output_qty,
    s.sale_qty                             AS sale_qty,
    s.stock_qty                            AS stock_qty,
    s.sale_amt                             AS sale_amt,
    ROUND(s.sale_qty / NULLIF(s.output_qty, 0), 4) AS sale_output_ratio,
    s.output_qty - s.sale_qty              AS stock_increase
FROM cdw.dws_产销存汇总 s
LEFT JOIN dim.dim_plant pl
       ON s.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
"""

ADS_税利分析 = """-- ADS 报表：税利分析（税利汇总 + 产销存汇总 + 牌号维表）
INSERT OVERWRITE TABLE ads.ads_税利分析 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                           AS plant_code,
    t.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    t.stat_month                           AS stat_month,
    t.total_tax_amt                        AS tax_amt,
    t.total_profit_amt                     AS profit_amt,
    t.total_tax_profit                     AS tax_profit,
    t.tax_profit_per_box                   AS tax_profit_per_box,
    COALESCE(p.sale_qty, 0)                AS sale_qty,
    CASE WHEN COALESCE(p.sale_qty, 0) > 0
         THEN ROUND(t.total_tax_profit / p.sale_qty, 2)
         ELSE 0 END                        AS tax_profit_per_sale_box
FROM cdw.dws_税利汇总 t
LEFT JOIN cdw.dws_产销存汇总 p
       ON t.plant_code = p.plant_code AND t.brand_code = p.brand_code
LEFT JOIN dim.dim_brand bd
       ON t.brand_code = bd.brand_code
WHERE t.dt = '2026-01-01';
"""

# 一个调度节点里的两条语句：先出经营指标明细，再汇总成驾驶舱宽表
ADS_驾驶舱 = """-- ADS 报表：经营指标驾驶舱（同一节点两条语句：明细 + 宽表）
CREATE TABLE ads.ads_经营指标明细 AS
SELECT
    m.plant_code                           AS plant_code,
    m.plant_name                           AS plant_name,
    m.brand_code                           AS brand_code,
    m.brand_name                           AS brand_name,
    m.output_qty                           AS output_qty,
    m.sale_qty                             AS sale_qty,
    m.sale_amt                             AS sale_amt,
    m.sale_output_ratio                    AS sale_output_ratio,
    t.tax_profit                           AS tax_profit,
    t.tax_profit_per_box                   AS tax_profit_per_box
FROM ads.ads_产销存月报 m
LEFT JOIN ads.ads_税利分析 t
       ON m.plant_code = t.plant_code AND m.brand_code = t.brand_code
WHERE m.dt = '2026-01-01';

INSERT OVERWRITE TABLE ads.ads_经营指标驾驶舱 PARTITION (dt = '2026-01-01')
SELECT
    i.plant_code                           AS plant_code,
    i.plant_name                           AS plant_name,
    SUM(i.output_qty)                      AS output_qty,
    SUM(i.sale_qty)                        AS sale_qty,
    SUM(i.sale_amt)                        AS sale_amt,
    SUM(i.tax_profit)                      AS tax_profit,
    ROUND(AVG(i.sale_output_ratio), 4)     AS avg_sale_output_ratio
FROM ads.ads_经营指标明细 i
WHERE i.dt = '2026-01-01'
GROUP BY i.plant_code, i.plant_name;
"""


WORKFLOWS: List[Dict[str, Any]] = [
    {
        "name": "wf_ods_采集",
        "description": "ODS 贴源采集：ERP/MES/WMS/FIN 源系统 -> 贴源表（4 个任务，并行执行）",
        "tasks": [
            {"name": "t_ods_产量流水", "type": "SQL", "sql": ODS_产量, "upstream": [],
             "description": "ERP 生产工单 -> ods.ods_卷烟产量流水"},
            {"name": "t_ods_销量流水", "type": "SQL", "sql": ODS_销量, "upstream": [],
             "description": "MES 销售出库 -> ods.ods_卷烟销量流水"},
            {"name": "t_ods_税利流水", "type": "SQL", "sql": ODS_税利, "upstream": [],
             "description": "FIN 税金凭证 -> ods.ods_税利上缴流水"},
            {"name": "t_ods_库存快照", "type": "SHELL", "raw_script": ODS_库存_SHELL, "upstream": [],
             "description": "WMS 库存快照 -> ods.ods_成品库存快照 + 库存基线（SHELL 任务，hive -e 多语句）"},
        ],
    },
    {
        "name": "wf_dwd_清洗",
        "description": "DWD 明细清洗：贴源表 + 维表 -> 明细事实表（4 个任务，并行执行）",
        "tasks": [
            {"name": "t_dwd_产量明细", "type": "SQL", "sql": DWD_产量, "upstream": [],
             "description": "产量明细事实表"},
            {"name": "t_dwd_销量明细", "type": "SQL", "sql": DWD_销量, "upstream": [],
             "description": "销量明细事实表"},
            {"name": "t_dwd_库存明细", "type": "SQL", "sql": DWD_库存, "upstream": [],
             "description": "成品库存明细事实表"},
            {"name": "t_dwd_税利明细", "type": "SQL", "sql": DWD_税利, "upstream": [],
             "description": "税利明细事实表"},
        ],
    },
    {
        "name": "wf_dws_汇总",
        "description": "DWS 汇总：明细 -> 汇总表（任务间有真实调度依赖：汇总任务等产量/库存汇总先跑完）",
        "tasks": [
            {"name": "t_dws_产量汇总", "type": "SQL", "sql": DWS_产量汇总, "upstream": [],
             "description": "产量汇总"},
            {"name": "t_dws_库存汇总", "type": "SQL", "sql": DWS_库存汇总, "upstream": [],
             "description": "库存汇总"},
            {"name": "t_dws_税利汇总", "type": "SQL", "sql": DWS_税利汇总, "upstream": [],
             "description": "税利汇总"},
            {"name": "t_dws_产销存汇总", "type": "SQL", "sql": DWS_产销存汇总,
             "upstream": ["t_dws_产量汇总", "t_dws_库存汇总"],
             "description": "产销存汇总（依赖产量汇总 / 库存汇总）"},
        ],
    },
    {
        "name": "wf_ads_报表",
        "description": "ADS 应用层：汇总表 -> 报表宽表（含一个 DEPENDENT 任务，声明对 wf_dws_汇总 的原生依赖）",
        "tasks": [
            {"name": "t_check_上游就绪", "type": "DEPENDENT", "depends_on_workflow": "wf_dws_汇总",
             "upstream": [], "description": "依赖检测：wf_dws_汇总 当天实例成功后才往下跑"},
            {"name": "t_ads_产销存月报", "type": "SQL", "sql": ADS_产销存月报,
             "upstream": ["t_check_上游就绪"], "description": "产销存月报"},
            {"name": "t_ads_税利分析", "type": "SQL", "sql": ADS_税利分析,
             "upstream": ["t_check_上游就绪"], "description": "税利分析"},
            {"name": "t_ads_经营指标驾驶舱", "type": "SQL", "sql": ADS_驾驶舱,
             "upstream": ["t_ads_产销存月报", "t_ads_税利分析"],
             "description": "经营指标驾驶舱（两段 SQL：明细 + 宽表）"},
        ],
    },
]


# --------------------------------------------------------------------------- #
# 建任务定义
# --------------------------------------------------------------------------- #
def build_task_definition(spec: Dict[str, Any], code: int, workflow_name: str,
                          workflow_codes: Dict[str, str]) -> Dict[str, Any]:
    """按任务类型拼海豚的 taskDefinition 结构。"""
    ttype = spec["type"]
    if ttype == "SQL":
        params: Dict[str, Any] = {
            "localParams": [],
            "resourceList": [],
            "type": "HIVE",
            "datasource": 1,                 # 由调用方补成真实数据源 id
            "sql": spec["sql"],
            "sqlType": "1",                  # 0=查询 1=非查询
            "preStatements": [],
            "postStatements": [],
            "displayRows": 10,
        }
    elif ttype == "SHELL":
        params = {"localParams": [], "resourceList": [], "rawScript": spec["raw_script"]}
    elif ttype == "DEPENDENT":
        target_code = int(workflow_codes[spec["depends_on_workflow"]])
        params = {
            "localParams": [], "resourceList": [],
            "dependence": {
                "relation": "AND",
                "dependTaskList": [{
                    "relation": "AND",
                    "dependItemList": [{
                        "projectCode": 0,            # 由调用方补成真实 project code
                        "definitionCode": target_code,
                        "depTaskCode": 0,            # 0 = 依赖整个工作流
                        "cycle": "day",
                        "dateValue": "today",
                    }],
                }],
            },
        }
    else:  # pragma: no cover - 防御
        raise ValueError(f"不支持的任务类型：{ttype}")

    return {
        "code": code,
        "name": spec["name"],
        "version": 1,
        "description": spec.get("description") or f"{workflow_name} 的 {spec['name']} 节点",
        "delayTime": 0,
        "taskType": ttype,
        "taskParams": params,
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


def build_relations(specs: Sequence[Dict[str, Any]], codes: Dict[str, int],
                    versions: Dict[str, int]) -> List[Dict[str, Any]]:
    """把 ``upstream`` 声明翻译成海豚的 processTaskRelationList。"""
    relations: List[Dict[str, Any]] = []
    for spec in specs:
        post = codes[spec["name"]]
        upstream = spec.get("upstream") or []
        if not upstream:
            relations.append({"name": "", "preTaskCode": 0, "postTaskCode": post,
                              "preTaskVersion": 0, "postTaskVersion": versions[spec["name"]],
                              "conditionType": "NONE", "conditionParams": {}})
            continue
        for up in upstream:
            relations.append({"name": "", "preTaskCode": codes[up], "postTaskCode": post,
                              "preTaskVersion": versions[up],
                              "postTaskVersion": versions[spec["name"]],
                              "conditionType": "NONE", "conditionParams": {}})
    return relations


# --------------------------------------------------------------------------- #
# 数据源 / 项目
# --------------------------------------------------------------------------- #
def ensure_datasource(client: DsClient) -> int:
    """确保有一个 HIVE 数据源（SQL 任务要绑数据源 id；只用于演示，不校验连通性）。"""
    for ds in client.list_datasources():
        if ds.get("name") == DEMO_DATASOURCE:
            print(f"[数据源] 复用已有数据源 {DEMO_DATASOURCE}（id={ds.get('id')}）")
            return int(ds["id"])
    created = client.create_datasource({
        "name": DEMO_DATASOURCE,
        "note": "P3 演示用 HIVE 数据源（不校验连通性，只给 SQL 任务挂个 id）",
        "type": "HIVE",
        "host": "localhost",
        "port": 10000,
        "database": "default",
        "userName": "hive",
        "password": "hive",
        "other": {"hive.server2.auth": "NONE"},
    })
    print(f"[数据源] 新建数据源 {DEMO_DATASOURCE}（id={created.get('id')}）")
    return int(created["id"])


def _delete_project(client: DsClient, code: Any, name: str) -> None:
    """先删干净项目下的工作流定义，再删项目（海豚不允许直接删有定义的项目）。

    注意：海豚的 ``DELETE /process-definition`` **要求定义处于 OFFLINE**，否则接口报错、
    定义留在库里，接着 ``DELETE /project`` 也会失败（项目非空），最后建同名项目时撞上
    「already exists」——整轮演示数据重建就废了。所以这里先无条件下线一次再删。
    """
    try:
        defs = client.list_process_definitions(code)
    except DsError as exc:                                   # pragma: no cover - 防御
        print(f"[项目] 读取 {name} 的工作流失败：{exc}", file=sys.stderr)
        defs = []
    for d in defs:
        try:
            client.request("POST", f"/projects/{int(code)}/process-definition/{int(d['code'])}/release",
                           params={"releaseState": "OFFLINE"}, raise_on_error=False)
        except DsError:                                      # pragma: no cover - 防御
            pass
        client.delete_process_definition(code, d["code"])
    print(f"[项目] 删除项目 {name}（code={code}），连带清理 {len(defs)} 个工作流定义")
    client.delete_project(code)


def recreate_project(client: DsClient, name: str) -> Dict[str, Any]:
    """建项目；同名项目已存在则先删（含其工作流定义）再建，保证脚本可重复执行。"""
    for proj in client.list_projects():
        if proj.get("name") == name:
            _delete_project(client, proj["code"], name)
            break
    created = client.create_project(name, DEMO_PROJECT_DESC)
    print(f"[项目] 已创建项目 {created['name']}（code={created['code']}）")
    return created


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python demos/ds_setup_demo.py",
        description="在本地 DolphinScheduler 上创建 P3 演示项目 / 工作流 / 任务，并导出定义存档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--base-url", default=None, help="海豚地址，默认 DS_BASE_URL 或内置默认值")
    ap.add_argument("--user", default=None, help="登录用户，默认 DS_USER（admin）")
    ap.add_argument("--password", default=None, help="登录密码，默认 DS_PASSWORD")
    ap.add_argument("--project-name", default=DEMO_PROJECT, help=f"项目名，默认 {DEMO_PROJECT}")
    ap.add_argument("--export-dir", default=str(PROJECT_ROOT / "docs" / "ds_demo_workflows"),
                    help="工作流定义导出目录，默认 docs/ds_demo_workflows")
    ap.add_argument("--no-export", action="store_true", help="不导出定义 JSON")
    ap.add_argument("--only", action="append", default=[], metavar="WORKFLOW",
                    help="只建指定工作流（可重复，调试用）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    client = DsClient(base_url=args.base_url, user=args.user, password=args.password)
    try:
        client.login()
    except DsConnectionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except DsAuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        datasource_id = ensure_datasource(client)
        project = recreate_project(client, args.project_name)
        project_code = int(project["code"])

        specs = [wf for wf in WORKFLOWS if not args.only or wf["name"] in args.only]
        workflow_codes: Dict[str, str] = {}
        created_summary: List[Dict[str, Any]] = []

        # 因为 ADS 工作流里的 DEPENDENT 任务要引用 DWS 工作流的 code，
        # 这里按 ODS -> DWD -> DWS -> ADS 的顺序建，先建好后把 code 记下来。
        for wf in specs:
            codes = client.gen_task_codes(project_code, len(wf["tasks"]))
            if len(codes) != len(wf["tasks"]):
                print(f"错误：申请 task code 失败（期望 {len(wf['tasks'])} 个，实得 {len(codes)} 个）",
                      file=sys.stderr)
                return 2
            code_of = {spec["name"]: codes[i] for i, spec in enumerate(wf["tasks"])}
            versions = {spec["name"]: 1 for spec in wf["tasks"]}
            task_defs = []
            for spec in wf["tasks"]:
                tdef = build_task_definition(spec, code_of[spec["name"]], wf["name"], workflow_codes)
                params = tdef["taskParams"]
                if "datasource" in params:
                    params["datasource"] = datasource_id
                if tdef["taskType"] == "DEPENDENT":
                    params["dependence"]["dependTaskList"][0]["dependItemList"][0]["projectCode"] = project_code
                task_defs.append(tdef)
            relations = build_relations(wf["tasks"], code_of, versions)

            created = client.create_process_definition(
                project_code=project_code,
                name=wf["name"],
                description=wf["description"],
                task_definitions=task_defs,
                task_relations=relations,
            )
            workflow_codes[wf["name"]] = str(created["code"])
            created_summary.append({
                "workflow": wf["name"],
                "code": str(created["code"]),
                "description": wf["description"],
                "tasks": [{"name": s["name"], "type": s["type"], "code": str(code_of[s["name"]]),
                           "upstream": s.get("upstream") or []} for s in wf["tasks"]],
            })
            print(f"[工作流] 已创建 {wf['name']}（code={created['code']}）"
                  f"，任务 {len(wf['tasks'])} 个：{'、'.join(s['name'] for s in wf['tasks'])}")

        if not args.no_export:
            export_dir = Path(args.export_dir)
            # 先清掉上一轮导出，避免任务改名 / 删除后留下陈旧文件
            if export_dir.exists():
                for stale in export_dir.glob("*.json"):
                    stale.unlink()
                sql_root = export_dir / "sql"
                if sql_root.exists():
                    shutil.rmtree(sql_root)
            export_dir.mkdir(parents=True, exist_ok=True)
            sql_dir = export_dir / "sql"
            sql_dir.mkdir(parents=True, exist_ok=True)
            for item in created_summary:
                detail = client.get_process_definition(project_code, int(item["code"]))
                (export_dir / f"{item['workflow']}.json").write_text(
                    json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                wf_dir = sql_dir / item["workflow"]
                wf_dir.mkdir(parents=True, exist_ok=True)
                for tdef in detail["taskDefinitionList"]:
                    scripts = extract_scripts(tdef)
                    for i, script in enumerate(scripts, 1):
                        suffix = "" if len(scripts) == 1 else f"_{i}"
                        (wf_dir / f"{tdef['name']}{suffix}.sql").write_text(
                            f"-- 来源：{item['workflow']} / {tdef['name']}（{tdef['taskType']}）"
                            f"，抽取自 {script['key']}\n{script['text']}\n", encoding="utf-8")
            manifest = {
                "note": "由 demos/ds_setup_demo.py 从真实 DolphinScheduler 实例导出（GET /process-definition/{code}）",
                "base_url": client.base_url,
                "project": {"name": project["name"], "code": str(project_code)},
                "datasource": {"name": DEMO_DATASOURCE, "id": datasource_id},
                "workflow_count": len(created_summary),
                "task_count": sum(len(w["tasks"]) for w in created_summary),
                "workflows": created_summary,
            }
            (export_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[导出] 工作流定义 + 任务 SQL 已导出到 {export_dir}")

        print("\n完成：")
        print(f"  项目        : {project['name']}（code={project_code}）")
        print(f"  工作流      : {len(created_summary)} 个")
        print(f"  任务节点    : {sum(len(w['tasks']) for w in created_summary)} 个")
        print("  下一步      : .venv/bin/python -m lineage.cli ds sync --graph-out ds_lineage.json")
        return 0
    except DsApiError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
