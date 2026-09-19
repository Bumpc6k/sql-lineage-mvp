-- =============================================================
-- ODS 层：财务税金凭证 → 贴源表 ods.ods_税利上缴流水
-- 上游：src.fin_税金凭证
-- 下游：cdw.dwd_税利明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_税利上缴流水 PARTITION (dt = '2026-01-01')
SELECT
    d.voucher_no        AS voucher_no,         -- 凭证号
    d.plant_code        AS plant_code,         -- 缴纳厂
    d.brand_code        AS brand_code,         -- 关联牌号
    d.tax_type          AS tax_type,           -- 税种（消费税/增值税/附加税）
    d.stat_month        AS stat_month,         -- 所属月份
    CAST(d.tax_amt AS DECIMAL(18, 2))          AS tax_amt,         -- 税额（元）
    CAST(d.profit_amt AS DECIMAL(18, 2))       AS profit_amt,      -- 利润（元）
    d.update_time       AS update_time
FROM src.fin_税金凭证 d
WHERE d.dt = '2026-01-01'
  AND d.stat_month >= '2026-01';
