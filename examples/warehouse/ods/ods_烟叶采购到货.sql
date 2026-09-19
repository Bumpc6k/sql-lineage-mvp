-- =============================================================
-- ODS 层：SRM 采购到货单 → 贴源表 ods.ods_烟叶采购到货
-- 上游：src.srm_采购到货单
-- 下游：cdw.dwd_烟叶采购明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_烟叶采购到货 PARTITION (dt = '2026-01-01')
SELECT
    e.purchase_no       AS purchase_no,        -- 采购单号
    e.supplier_code     AS supplier_code,      -- 供应商编码
    e.leaf_grade        AS leaf_grade,         -- 烟叶等级
    e.origin_area       AS origin_area,        -- 产地
    e.receive_date      AS receive_date,       -- 到货日期
    CAST(e.leaf_weight AS DECIMAL(18, 4))      AS leaf_weight,     -- 到货重量（担）
    CAST(e.unit_price AS DECIMAL(18, 2))       AS unit_price,      -- 单价（元/担）
    e.update_time       AS update_time
FROM src.srm_采购到货单 e
WHERE e.dt = '2026-01-01'
  AND e.leaf_weight > 0;
