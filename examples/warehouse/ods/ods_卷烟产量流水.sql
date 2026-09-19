-- =============================================================
-- ODS 层：卷烟生产工单 → 贴源表 ods.ods_卷烟产量流水
-- 上游：src.erp_生产工单明细（ERP 源系统接口表）
-- 下游：cdw.dwd_卷烟产量明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')
SELECT
    a.work_order_no     AS work_order_no,      -- 工单号
    a.plant_code        AS plant_code,         -- 生产厂编码
    a.brand_code        AS brand_code,         -- 牌号编码
    a.work_date         AS work_date,          -- 生产日期
    CAST(a.output_qty AS DECIMAL(18, 4))       AS output_qty,     -- 产量（箱）
    CAST(a.defect_qty AS DECIMAL(18, 4))       AS defect_qty,     -- 不良品量（箱）
    a.shift_code        AS shift_code,         -- 班次
    a.update_time       AS update_time
FROM src.erp_生产工单明细 a
WHERE a.dt = '2026-01-01'
  AND a.work_date >= '2026-01-01'
  AND a.output_qty IS NOT NULL;
