-- 来源：wf_ads_报表 / t_ads_产销存月报（SQL），抽取自 taskParams.sql
-- ADS 报表：产销存月报（产销存汇总 + 工厂 / 牌号维表）
INSERT OVERWRITE TABLE ads.ads_产销存月报 PARTITION (dt = '2026-01-01')
SELECT
    s.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    s.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    bd.price_band                          AS price_band,
    s.output_qty                           AS output_qty,
    s.sale_qty                             AS sale_qty,
    s.stock_qty                            AS stock_qty,
    s.sale_amt                             AS sale_amt,
    ROUND(s.sale_qty / NULLIF(s.output_qty, 0), 4) AS sale_output_ratio,
    s.output_qty - s.sale_qty              AS stock_increase
FROM cdw.dws_产销存汇总 s
LEFT JOIN dim.dim_plant pl
       ON s.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
