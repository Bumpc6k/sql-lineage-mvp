-- 来源：wf_dwd_清洗 / t_dwd_税利明细（SQL），抽取自 taskParams.sql
-- DWD 明细：税利贴源 + 牌号维表 -> cdw.dwd_税利明细
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
