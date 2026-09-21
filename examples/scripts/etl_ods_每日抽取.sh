#!/bin/bash
# =============================================================================
# 烟草数仓 ODS 层每日抽取（Shell 脚本示例）
# 说明：这是真实的「Shell 里嵌 SQL」写法集合，用来验证 script_parser 的提取能力。
#   1) hive -e "多语句 SQL"
#   2) SQL 写在 shell 变量里，再 hive -e "$SQL" 引用
#   3) hive <<EOF heredoc
#   4) spark-sql -f etl/xxx.sql（SQL 在外部文件里，脚本内没有正文）
#   5) echo / 变量赋值 等非 SQL 内容（不应被当成 SQL）
# =============================================================================
set -euo pipefail

DT=$(date -d "yesterday" +%Y-%m-%d)
echo "[INFO] 开始抽取 ODS 层，分区 dt=${DT}"

# ---- 1) 产量流水：hive -e 内嵌多语句 SQL -----------------------------------
hive -e "
INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '${DT}')
SELECT a.work_order_no AS work_order_no,
       a.plant_code    AS plant_code,
       a.brand_code    AS brand_code,
       a.output_qty    AS output_qty,
       a.defect_qty    AS defect_qty
FROM src.erp_生产工单明细 a
WHERE a.dt = '${DT}';

INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '${DT}')
SELECT c.warehouse_code AS warehouse_code,
       c.plant_code     AS plant_code,
       c.stock_qty      AS stock_qty
FROM src.wms_库存快照 c
WHERE c.dt = '${DT}';
"

# ---- 2) 库存基线：SQL 放在 shell 变量里，命令里引用变量 ---------------------
SQL_STOCK="INSERT OVERWRITE TABLE ods.ods_库存基线 PARTITION (dt = '${DT}')
SELECT c.plant_code AS plant_code,
       SUM(c.stock_qty) AS stock_qty
FROM src.wms_库存快照 c
WHERE c.dt = '${DT}'
GROUP BY c.plant_code;"
beeline -e "$SQL_STOCK"

# ---- 3) 税利上缴：heredoc --------------------------------------------------
hive <<EOF
INSERT OVERWRITE TABLE ods.ods_税利上缴流水 PARTITION (dt = '${DT}')
SELECT t.tax_no     AS tax_no,
       t.tax_amt    AS tax_amt,
       t.tax_date   AS tax_date
FROM src.erp_税利上缴 t
WHERE t.dt = '${DT}';
EOF

# ---- 4) 设备工况：SQL 在外部文件里（脚本内没有正文，只记未解析提示） -------
spark-sql -f etl/ods_设备运行工况.sql

echo "[INFO] ODS 层抽取结束，分区 dt=${DT}"
