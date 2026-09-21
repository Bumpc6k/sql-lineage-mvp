-- =============================================================
-- 样本 16：LEFT SEMI JOIN / LEFT ANTI JOIN + DISTINCT
-- 特性：半连接（EXISTS 语义）、反连接（NOT EXISTS 语义）
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_已产销工单明细 PARTITION (dt = '2026-01-01')
SELECT DISTINCT
    p.work_order_no AS work_order_no,
    p.plant_code    AS plant_code,
    p.output_qty    AS output_qty
FROM ods.ods_卷烟产量流水 p
LEFT SEMI JOIN ods.ods_卷烟销量流水 s
            ON p.plant_code = s.plant_code
           AND p.brand_code = s.brand_code
LEFT ANTI JOIN dim.dim_brand b
            ON p.brand_code = b.brand_code
WHERE p.dt = '2026-01-01';
