-- =============================================================
-- ODS 层：WMS 库存快照 → 贴源表 ods.ods_成品库存快照
-- 上游：src.wms_库存快照
-- 下游：cdw.dwd_成品库存明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_成品库存快照 PARTITION (dt = '2026-01-01')
SELECT
    c.warehouse_code    AS warehouse_code,     -- 仓库编码
    c.plant_code        AS plant_code,         -- 所属厂
    c.brand_code        AS brand_code,         -- 牌号编码
    c.stock_date        AS stock_date,         -- 快照日期
    CAST(c.stock_qty AS DECIMAL(18, 4))        AS stock_qty,       -- 库存量（箱）
    CAST(c.stock_amt AS DECIMAL(18, 2))        AS stock_amt,       -- 库存金额（元）
    c.batch_no          AS batch_no,           -- 批次号
    c.update_time       AS update_time
FROM src.wms_库存快照 c
WHERE c.dt = '2026-01-01'
  AND c.warehouse_code IS NOT NULL;
