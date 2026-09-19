-- 来源：wf_ads_报表 / t_ads_经营指标驾驶舱（SQL），抽取自 taskParams.sql
-- ADS 报表：经营指标驾驶舱（同一节点两条语句：明细 + 宽表）
CREATE TABLE ads.ads_经营指标明细 AS
SELECT
    m.plant_code                           AS plant_code,
    m.plant_name                           AS plant_name,
    m.brand_code                           AS brand_code,
    m.brand_name                           AS brand_name,
    m.output_qty                           AS output_qty,
    m.sale_qty                             AS sale_qty,
    m.sale_amt                             AS sale_amt,
    m.sale_output_ratio                    AS sale_output_ratio,
    t.tax_profit                           AS tax_profit,
    t.tax_profit_per_box                   AS tax_profit_per_box
FROM ads.ads_产销存月报 m
LEFT JOIN ads.ads_税利分析 t
       ON m.plant_code = t.plant_code AND m.brand_code = t.brand_code
WHERE m.dt = '2026-01-01';

INSERT OVERWRITE TABLE ads.ads_经营指标驾驶舱 PARTITION (dt = '2026-01-01')
SELECT
    i.plant_code                           AS plant_code,
    i.plant_name                           AS plant_name,
    SUM(i.output_qty)                      AS output_qty,
    SUM(i.sale_qty)                        AS sale_qty,
    SUM(i.sale_amt)                        AS sale_amt,
    SUM(i.tax_profit)                      AS tax_profit,
    ROUND(AVG(i.sale_output_ratio), 4)     AS avg_sale_output_ratio
FROM ads.ads_经营指标明细 i
WHERE i.dt = '2026-01-01'
GROUP BY i.plant_code, i.plant_name;
