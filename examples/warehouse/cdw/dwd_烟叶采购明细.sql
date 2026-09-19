-- =============================================================
-- DWD 层：贴源采购 → 明细事实 dwd.dwd_烟叶采购明细
-- 上游：ods.ods_烟叶采购到货 + dim.dim_supplier
-- 下游：cdw.dws_烟叶采购供应商汇总
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_烟叶采购明细 PARTITION (dt = '2026-01-01')
SELECT
    c.purchase_no                          AS purchase_no,
    c.receive_date                         AS receive_date,
    c.supplier_code                        AS supplier_code,
    sp.supplier_name                       AS supplier_name,
    sp.supplier_level                      AS supplier_level,
    c.leaf_grade                           AS leaf_grade,
    c.origin_area                          AS origin_area,
    c.leaf_weight                          AS leaf_weight,
    c.unit_price                           AS unit_price,
    ROUND(c.leaf_weight * c.unit_price, 2) AS total_amt
FROM ods.ods_烟叶采购到货 c
LEFT JOIN dim.dim_supplier sp
       ON c.supplier_code = sp.supplier_code
WHERE c.dt = '2026-01-01';
