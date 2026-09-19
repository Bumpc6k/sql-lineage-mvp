-- =============================================================
-- 示例 5：一个文件多条语句 —— ODS -> DWD -> DWS 完整加工链路
-- 场景：模拟调度平台（DolphinScheduler）里一个任务节点内的多段 SQL
-- 覆盖形态：一个文件内多条语句，每条独立产出 task_type / 血缘
-- 预期的表级链路：ods_* -> dwd_* -> dws_*
-- =============================================================

-- [1/3] ODS 烟叶采购 -> DWD 烟叶采购明细
CREATE TABLE dwd.dwd_烟叶采购明细 AS
SELECT
    c.purchase_id       AS purchase_id,
    c.supplier_code     AS supplier_code,
    c.leaf_weight       AS leaf_weight,
    c.unit_price        AS unit_price,
    c.leaf_weight * c.unit_price AS total_amt,
    c.purchase_date     AS purchase_date
FROM ods.ods_烟叶采购 c
WHERE c.dt = '2026-01-01'
  AND c.leaf_weight IS NOT NULL;

-- [2/3] DWD 烟叶采购明细 -> DWS 烟叶采购供应商汇总
INSERT OVERWRITE TABLE dws.dws_烟叶采购供应商汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.supplier_code                AS supplier_code,
    COUNT(1)                       AS order_cnt,
    SUM(d.leaf_weight)             AS total_weight,
    SUM(d.total_amt)               AS total_amt,
    AVG(d.unit_price)              AS avg_unit_price
FROM dwd.dwd_烟叶采购明细 d
WHERE d.purchase_date >= '2026-01-01'
GROUP BY d.supplier_code;

-- [3/3] DWS -> ADS 供应商排名（带明细子查询）
INSERT OVERWRITE TABLE ads.ads_烟叶供应商排名 PARTITION (dt = '2026-01-01')
SELECT
    s.supplier_code            AS supplier_code,
    sup.supplier_name          AS supplier_name,
    s.total_weight             AS total_weight,
    s.total_amt                AS total_amt,
    s.avg_unit_price           AS avg_unit_price,
    ROW_NUMBER() OVER (ORDER BY s.total_amt DESC) AS rank_no
FROM dws.dws_烟叶采购供应商汇总 s
LEFT JOIN dim.dim_supplier sup
       ON s.supplier_code = sup.supplier_code
WHERE s.dt = '2026-01-01';
