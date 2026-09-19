-- =============================================================
-- DWD 层：贴源库存 → 明细事实 dwd.dwd_成品库存明细
-- 上游：ods.ods_成品库存快照 + dim.dim_plant
-- 下游：cdw.dws_库存汇总
-- =============================================================
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
