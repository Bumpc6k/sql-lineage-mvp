-- =============================================================
-- ADS 层：产销存月报（应用层报表）
-- 上游：cdw.dws_产销存汇总 + dim.dim_plant + dim.dim_brand
-- 下游：ads.ads_经营指标明细
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_产销存月报 PARTITION (dt = '2026-01-01')
SELECT
    s.plant_code                                          AS plant_code,
    pl.plant_name                                         AS plant_name,
    s.brand_code                                          AS brand_code,
    bd.brand_name                                         AS brand_name,
    bd.price_band                                         AS price_band,
    s.output_qty                                          AS output_qty,
    s.sale_qty                                            AS sale_qty,
    s.stock_qty                                           AS stock_qty,
    s.sale_amt                                            AS sale_amt,
    ROUND(s.sale_qty / NULLIF(s.output_qty, 0), 4)        AS sale_output_ratio,   -- 产销率
    s.output_qty - s.sale_qty                             AS stock_increase        -- 库存增量
FROM cdw.dws_产销存汇总 s
LEFT JOIN dim.dim_plant pl
       ON s.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
