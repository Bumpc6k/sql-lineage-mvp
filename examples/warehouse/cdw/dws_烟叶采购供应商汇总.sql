-- =============================================================
-- DWS 层：烟叶采购供应商汇总
-- 上游：cdw.dwd_烟叶采购明细
-- 下游：ads.ads_烟叶供应商排名
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_烟叶采购供应商汇总 PARTITION (dt = '2026-01-01')
SELECT
    c.supplier_code                       AS supplier_code,
    c.supplier_name                       AS supplier_name,
    c.supplier_level                      AS supplier_level,
    COUNT(1)                              AS purchase_cnt,
    SUM(c.leaf_weight)                    AS total_weight,
    SUM(c.total_amt)                      AS total_amt,
    ROUND(AVG(c.unit_price), 2)           AS avg_unit_price,
    MAX(c.leaf_grade)                     AS best_leaf_grade
FROM cdw.dwd_烟叶采购明细 c
WHERE c.dt = '2026-01-01'
GROUP BY c.supplier_code, c.supplier_name, c.supplier_level;
