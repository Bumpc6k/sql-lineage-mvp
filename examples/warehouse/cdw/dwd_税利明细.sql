-- =============================================================
-- DWD 层：贴源税利 → 明细事实 dwd.dwd_税利明细
-- 上游：ods.ods_税利上缴流水 + dim.dim_brand
-- 下游：cdw.dws_税利汇总
-- =============================================================
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
    t.tax_amt + t.profit_amt               AS total_tax_profit   -- 税利总额
FROM ods.ods_税利上缴流水 t
LEFT JOIN dim.dim_brand bd
       ON t.brand_code = bd.brand_code
WHERE t.dt = '2026-01-01';
