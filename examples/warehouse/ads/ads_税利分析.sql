-- =============================================================
-- ADS 层：税利分析（应用层报表）
-- 上游：cdw.dws_税利汇总 + cdw.dws_产销存汇总 + dim.dim_brand
-- 下游：ads.ads_经营指标明细
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_税利分析 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                                     AS plant_code,
    t.brand_code                                     AS brand_code,
    bd.brand_name                                    AS brand_name,
    t.stat_month                                     AS stat_month,
    t.total_tax_amt                                  AS tax_amt,
    t.total_profit_amt                               AS profit_amt,
    t.total_tax_profit                               AS tax_profit,
    t.tax_profit_per_box                             AS tax_profit_per_box,
    COALESCE(p.sale_qty, 0)                          AS sale_qty,
    CASE WHEN COALESCE(p.sale_qty, 0) > 0
         THEN ROUND(t.total_tax_profit / p.sale_qty, 2)
         ELSE 0 END                                  AS tax_profit_per_sale_box
FROM cdw.dws_税利汇总 t
LEFT JOIN cdw.dws_产销存汇总 p
       ON t.plant_code = p.plant_code AND t.brand_code = p.brand_code
LEFT JOIN dim.dim_brand bd
       ON t.brand_code = bd.brand_code
WHERE t.dt = '2026-01-01';
