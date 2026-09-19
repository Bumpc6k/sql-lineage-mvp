-- 来源：wf_dwd_清洗 / t_dwd_产量明细（SQL），抽取自 taskParams.sql
-- DWD 明细：产量贴源 + 工厂 / 牌号维表 -> cdw.dwd_卷烟产量明细
INSERT OVERWRITE TABLE cdw.dwd_卷烟产量明细 PARTITION (dt = '2026-01-01')
SELECT
    p.work_order_no                        AS work_order_no,
    p.work_date                            AS work_date,
    p.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    p.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    bd.price_band                          AS price_band,
    p.output_qty                           AS output_qty,
    p.output_qty * 250                     AS output_qty_cig,
    p.defect_qty                           AS defect_qty,
    CASE WHEN p.output_qty > 0
         THEN ROUND(p.defect_qty / p.output_qty, 6)
         ELSE 0 END                        AS defect_rate,
    p.shift_code                           AS shift_code
FROM ods.ods_卷烟产量流水 p
LEFT JOIN dim.dim_plant pl
       ON p.plant_code = pl.plant_code
LEFT JOIN dim.dim_brand bd
       ON p.brand_code = bd.brand_code
WHERE p.dt = '2026-01-01';
