-- =============================================================
-- 样本 01：多层 CTE（b 引用 a，c 引用 b；外层再 JOIN 维表）
-- 特性：WITH a AS (...), b AS (...), c AS (...) 链式引用
-- =============================================================
WITH base AS (
    SELECT o.plant_code  AS plant_code,
           o.brand_code  AS brand_code,
           o.output_qty  AS output_qty,
           o.work_date   AS work_date
    FROM ods.ods_卷烟产量流水 o
    WHERE o.dt = '2026-01-01'
),
agg AS (
    SELECT b.plant_code AS plant_code,
           SUM(b.output_qty) AS output_qty,
           MAX(b.work_date)  AS last_work_date
    FROM base b
    GROUP BY b.plant_code
),
jnd AS (
    SELECT a.plant_code     AS plant_code,
           a.output_qty     AS output_qty,
           a.last_work_date AS last_work_date,
           p.plant_name     AS plant_name
    FROM agg a
    LEFT JOIN dim.dim_plant p ON a.plant_code = p.plant_code
)
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT plant_code, output_qty, last_work_date, plant_name
FROM jnd;
