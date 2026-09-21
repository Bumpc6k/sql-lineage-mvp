-- =============================================================
-- 样本 15：GROUPING SETS（多维汇总）+ ROLLUP / CUBE
-- 特性：GROUP BY ... GROUPING SETS (...)，含空分组集 ()
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_多维产销分析 PARTITION (dt = '2026-01-01')
SELECT
    COALESCE(o.plant_code, 'ALL') AS plant_code,
    COALESCE(o.brand_code, 'ALL') AS brand_code,
    SUM(o.output_qty)             AS output_qty,
    COUNT(DISTINCT o.work_order_no) AS wo_cnt
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
GROUP BY o.plant_code, o.brand_code
GROUPING SETS ((o.plant_code, o.brand_code), (o.plant_code), ());

INSERT OVERWRITE TABLE ads.ads_产量ROLLUP PARTITION (dt = '2026-01-01')
SELECT
    o.plant_code      AS plant_code,
    o.brand_code      AS brand_code,
    SUM(o.output_qty) AS output_qty
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
GROUP BY ROLLUP (o.plant_code, o.brand_code);

INSERT OVERWRITE TABLE ads.ads_产量CUBE PARTITION (dt = '2026-01-01')
SELECT
    o.plant_code      AS plant_code,
    o.brand_code      AS brand_code,
    SUM(o.output_qty) AS output_qty
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
GROUP BY CUBE (o.plant_code, o.brand_code);
