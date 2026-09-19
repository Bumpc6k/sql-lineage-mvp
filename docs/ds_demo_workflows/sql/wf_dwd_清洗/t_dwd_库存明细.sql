-- 来源：wf_dwd_清洗 / t_dwd_库存明细（SQL），抽取自 taskParams.sql
-- DWD 明细：库存快照 + 工厂维表 -> cdw.dwd_成品库存明细
INSERT OVERWRITE TABLE cdw.dwd_成品库存明细 PARTITION (dt = '2026-01-01')
SELECT
    k.warehouse_code                       AS warehouse_code,
    k.stock_date                           AS stock_date,
    k.plant_code                           AS plant_code,
    pl.plant_name                          AS plant_name,
    k.brand_code                           AS brand_code,
    k.batch_no                             AS batch_no,
    k.stock_qty                            AS stock_qty,
    k.stock_amt                            AS stock_amt,
    ROUND(k.stock_amt / NULLIF(k.stock_qty, 0), 2) AS stock_unit_cost
FROM ods.ods_成品库存快照 k
LEFT JOIN dim.dim_plant pl
       ON k.plant_code = pl.plant_code
WHERE k.dt = '2026-01-01';
