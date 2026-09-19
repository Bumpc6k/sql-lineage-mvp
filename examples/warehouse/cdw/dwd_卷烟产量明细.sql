-- =============================================================
-- DWD 层：贴源产量 → 明细事实 dwd.dwd_卷烟产量明细
-- 上游：ods.ods_卷烟产量流水 + dim.dim_plant + dim.dim_brand
-- 下游：cdw.dws_产量汇总 / cdw.dws_税利汇总
-- =============================================================
CREATE TABLE IF NOT EXISTS cdw.dwd_卷烟产量明细
COMMENT '卷烟产量明细事实表'
AS
SELECT
    p.work_order_no                        AS work_order_no,
    p.work_date                            AS work_date,
    p.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    p.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    bd.price_band                          AS price_band,
    p.output_qty                           AS output_qty,
    p.output_qty * 250                     AS output_qty_cig,     -- 箱转条
    p.defect_qty                           AS defect_qty,
    CASE WHEN p.output_qty > 0
         THEN ROUND(p.defect_qty / p.output_qty, 6)
         ELSE 0 END                        AS defect_rate,        -- 不良品率
    p.shift_code                           AS shift_code
FROM ods.ods_卷烟产量流水 p
LEFT JOIN dim.dim_plant pl
       ON p.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON p.brand_code = bd.brand_code
WHERE p.dt = '2026-01-01';
