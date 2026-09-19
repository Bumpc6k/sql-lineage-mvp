-- 来源：wf_ods_采集 / t_ods_库存快照（SHELL），抽取自 taskParams.rawScript
INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '2026-01-01')
SELECT
    c.warehouse_code                       AS warehouse_code,
    c.plant_code                           AS plant_code,
    c.brand_code                           AS brand_code,
    c.stock_date                           AS stock_date,
    CAST(c.stock_qty AS DECIMAL(18, 4))    AS stock_qty,
    CAST(c.stock_amt AS DECIMAL(18, 2))    AS stock_amt,
    c.batch_no                             AS batch_no,
    c.update_time                          AS update_time
FROM src.wms_库存快照 c
WHERE c.dt = '2026-01-01'
  AND c.warehouse_code IS NOT NULL;
INSERT OVERWRITE TABLE ods.ods_库存快照基线 PARTITION (dt = '2026-01-01')
SELECT
    c.plant_code                           AS plant_code,
    c.brand_code                           AS brand_code,
    SUM(CAST(c.stock_qty AS DECIMAL(18, 4))) AS base_stock_qty
FROM src.wms_库存快照 c
WHERE c.dt = '2026-01-01'
  AND c.batch_no IS NOT NULL
GROUP BY c.plant_code, c.brand_code;
