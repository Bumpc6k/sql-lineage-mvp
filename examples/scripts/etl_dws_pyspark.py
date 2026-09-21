#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烟草数仓 DWS 层加工（PySpark 脚本示例）。

这是真实的「Python 脚本里嵌 SQL」写法集合，用来验证 script_parser 的提取能力：
  1) SQL 写在模块级变量（三引号）里，spark.sql(变量) 引用
  2) spark.sql(f'''...''') —— f-string 占位符（运行时才知道值）
  3) spark.sql('''...''') 直接内联
  4) pandas.read_sql(...) / sqlalchemy text(...)
  5) 非 SQL 字符串（表名、配置项）不应被当成 SQL
"""

import pandas as pd
from pyspark.sql import SparkSession
from sqlalchemy import text

DT = "2026-01-01"
SOURCE_DB = "ods"
TARGET_DB = "cdw"

spark = SparkSession.builder.appName("dws_产销存汇总").enableHiveSupport().getOrCreate()

# ---- 1) SQL 在模块级变量里 --------------------------------------------------
SQL_DWS_SALES = """
INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')
SELECT t.plant_code AS plant_code,
       t.biz_type   AS biz_type,
       SUM(t.qty)   AS qty
FROM (
    SELECT plant_code, '产量' AS biz_type, output_qty AS qty
      FROM ods.ods_卷烟产量流水 WHERE dt = '2026-01-01'
    UNION ALL
    SELECT plant_code, '销量' AS biz_type, sale_qty AS qty
      FROM ods.ods_卷烟销量流水 WHERE dt = '2026-01-01'
) t
GROUP BY t.plant_code, t.biz_type
"""
spark.sql(SQL_DWS_SALES)

# ---- 2) f-string：分区值 / 日期运行时才知道 ---------------------------------
spark.sql(f"""
INSERT OVERWRITE TABLE cdw.dws_产量日汇总 PARTITION (dt = '{DT}')
SELECT o.plant_code      AS plant_code,
       SUM(o.output_qty) AS output_qty,
       COUNT(1)          AS wo_cnt
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '{DT}'
GROUP BY o.plant_code
""")

# ---- 3) 直接内联的 SQL ------------------------------------------------------
spark.sql("""
INSERT OVERWRITE TABLE ads.ads_经营指标明细 PARTITION (dt = '2026-01-01')
SELECT d.plant_code AS plant_code,
       d.qty        AS qty,
       p.plant_name AS plant_name
FROM cdw.dws_产销存汇总 d
LEFT JOIN dim.dim_plant p ON d.plant_code = p.plant_code
WHERE d.dt = '2026-01-01'
""")

# ---- 4) pandas / sqlalchemy 里的 SQL ---------------------------------------
dim_df = pd.read_sql(
    "SELECT plant_code, plant_name FROM dim.dim_plant WHERE status = '1'",
    conn,
)

extra = pd.read_sql_query(text("SELECT tax_no, tax_amt FROM ods.ods_税利上缴流水"), conn)

# 非 SQL 字符串：不应被提取
print(f"[INFO] source={SOURCE_DB} target={TARGET_DB} dt={DT}")
