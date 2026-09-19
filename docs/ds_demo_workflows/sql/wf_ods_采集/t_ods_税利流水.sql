-- 来源：wf_ods_采集 / t_ods_税利流水（SQL），抽取自 taskParams.sql
-- ODS 贴源：财务税金凭证 -> ods.ods_税利上缴流水
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
