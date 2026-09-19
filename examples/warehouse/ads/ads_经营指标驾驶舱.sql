-- =============================================================
-- ADS 层（最末端）：经营指标驾驶舱
-- 一个调度节点内的两条语句：先出经营指标明细，再汇总成驾驶舱宽表
-- 上游：ads.ads_产销存月报 / ads.ads_税利分析 / ads.ads_烟叶供应商排名 / ads.ads_设备运行看板
-- 这是全图最深的链路（src -> ods -> dwd -> dws -> ads -> ads_经营指标明细 -> 驾驶舱）
-- =============================================================

-- [1/2] 经营指标明细
CREATE TABLE ads.ads_经营指标明细 AS
SELECT
    m.plant_code                       AS plant_code,
    m.plant_name                       AS plant_name,
    m.brand_code                       AS brand_code,
    m.brand_name                       AS brand_name,
    m.output_qty                       AS output_qty,
    m.sale_qty                         AS sale_qty,
    m.sale_amt                         AS sale_amt,
    m.sale_output_ratio                AS sale_output_ratio,
    t.tax_profit                       AS tax_profit,
    t.tax_profit_per_box               AS tax_profit_per_box
FROM ads.ads_产销存月报 m
LEFT JOIN ads.ads_税利分析 t
       ON m.plant_code = t.plant_code AND m.brand_code = t.brand_code
WHERE m.dt = '2026-01-01';

-- [2/2] 驾驶舱宽表（经营指标明细 + 烟叶采购 + 设备看板）
INSERT OVERWRITE TABLE ads.ads_经营指标驾驶舱 PARTITION (dt = '2026-01-01')
SELECT
    i.plant_code                       AS plant_code,
    i.plant_name                       AS plant_name,
    SUM(i.output_qty)                  AS output_qty,
    SUM(i.sale_qty)                    AS sale_qty,
    SUM(i.sale_amt)                    AS sale_amt,
    SUM(i.tax_profit)                  AS tax_profit,
    ROUND(AVG(i.sale_output_ratio), 4) AS avg_sale_output_ratio,
    MAX(s.total_amt)                   AS max_supplier_amt,
    MAX(e.avg_run_rate)                AS max_device_run_rate
FROM ads.ads_经营指标明细 i
LEFT JOIN ads.ads_烟叶供应商排名 s
       ON i.brand_code = s.supplier_code
LEFT JOIN ads.ads_设备运行看板 e
       ON i.plant_code = e.plant_code
WHERE i.dt = '2026-01-01'
GROUP BY i.plant_code, i.plant_name;
