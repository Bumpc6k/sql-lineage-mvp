-- =============================================================
-- 示例 2：DWD -> DWS（INSERT OVERWRITE + 多表 JOIN + 聚合）
-- 场景：卷烟产量明细关联厂维度、牌号维度，按厂/牌号/日期汇总
-- 覆盖形态：INSERT OVERWRITE TABLE ... PARTITION (...)、LEFT JOIN、
--           INNER JOIN、GROUP BY、别名表、SUM/COUNT 聚合
-- =============================================================
INSERT OVERWRITE TABLE dws.dws_卷烟产量汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                       AS plant_code,
    d.brand_code                       AS brand_code,
    p.plant_name                       AS plant_name,
    b.brand_name                       AS brand_name,
    SUM(d.output_qty)                  AS total_output_qty,
    SUM(d.output_qty_cig)              AS total_output_qty_cig,
    COUNT(1)                           AS record_cnt,
    MAX(d.prod_date)                   AS last_prod_date
FROM dwd.dwd_卷烟产量明细 d
LEFT JOIN dim.dim_plant p
       ON d.plant_code = p.plant_code
INNER JOIN dim.dim_brand b
        ON d.brand_code = b.brand_code
WHERE d.prod_date >= '2026-01-01'
  AND d.prod_date <= '2026-01-31'
GROUP BY
    d.plant_code,
    d.brand_code,
    p.plant_name,
    b.brand_name;
