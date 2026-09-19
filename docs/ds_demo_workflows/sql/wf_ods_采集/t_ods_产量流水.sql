-- 来源：wf_ods_采集 / t_ods_产量流水（SQL），抽取自 taskParams.sql
-- ODS 贴源：ERP 生产工单 -> ods.ods_卷烟产量流水
INSERT OVERWRITE TABLE ods.ods_卷烟产量流水 PARTITION (dt = '2026-01-01')
SELECT
    a.work_order_no                        AS work_order_no,
    a.plant_code                           AS plant_code,
    a.brand_code                           AS brand_code,
    a.work_date                            AS work_date,
    CAST(a.output_qty AS DECIMAL(18, 4))   AS output_qty,
    CAST(a.defect_qty AS DECIMAL(18, 4))   AS defect_qty,
    a.shift_code                           AS shift_code,
    a.update_time                          AS update_time
FROM src.erp_生产工单明细 a
WHERE a.dt = '2026-01-01'
  AND a.output_qty IS NOT NULL;
