-- 来源：wf_dws_汇总 / t_dws_税利汇总（SQL），抽取自 taskParams.sql
-- DWS 汇总：按厂 / 牌号 / 月份聚合税利，并算单箱税利
INSERT OVERWRITE TABLE cdw.dws_税利汇总 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                           AS plant_code,
    t.brand_code                           AS brand_code,
    t.stat_month                           AS stat_month,
    SUM(t.tax_amt)                         AS total_tax_amt,
    SUM(t.profit_amt)                      AS total_profit_amt,
    SUM(t.total_tax_profit)                AS total_tax_profit,
    MAX(p.total_output_qty)                AS total_output_qty,
    CASE WHEN MAX(p.total_output_qty) > 0
         THEN ROUND(SUM(t.total_tax_profit) / MAX(p.total_output_qty), 2)
         ELSE 0 END                        AS tax_profit_per_box
FROM cdw.dwd_税利明细 t
LEFT JOIN cdw.dws_产量汇总 p
       ON t.plant_code = p.plant_code AND t.brand_code = p.brand_code
WHERE t.dt = '2026-01-01'
GROUP BY t.plant_code, t.brand_code, t.stat_month;
