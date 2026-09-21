-- =============================================================
-- 样本 03：多表 JOIN（5 张表：LEFT / INNER / FULL OUTER / 再次 LEFT）
-- 特性：同一 SELECT 内 4 个 JOIN，含 FULL OUTER JOIN
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_生产综合宽表 PARTITION (dt = '2026-01-01')
SELECT
    p.work_order_no   AS work_order_no,
    p.plant_code      AS plant_code,
    pl.plant_name     AS plant_name,
    bd.brand_name     AS brand_name,
    e.runtime_hours   AS runtime_hours,
    s.sale_qty        AS sale_qty,
    p.output_qty      AS output_qty
FROM ods.ods_卷烟产量流水 p
LEFT JOIN dim.dim_plant pl
       ON p.plant_code = pl.plant_code
INNER JOIN dim.dim_brand bd
        ON p.brand_code = bd.brand_code
FULL OUTER JOIN ods.ods_设备运行工况 e
             ON p.work_order_no = e.work_order_no
LEFT JOIN ods.ods_卷烟销量流水 s
       ON p.plant_code = s.plant_code
WHERE p.dt = '2026-01-01';
