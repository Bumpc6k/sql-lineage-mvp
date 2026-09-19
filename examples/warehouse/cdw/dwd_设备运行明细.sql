-- =============================================================
-- DWD 层：贴源工况 → 明细事实 dwd.dwd_设备运行明细
-- 上游：ods.ods_设备运行工况 + dim.dim_equipment
-- 下游：cdw.dws_设备效率汇总
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_设备运行明细 PARTITION (dt = '2026-01-01')
SELECT
    eq.device_code                         AS device_code,
    eq.device_type                         AS device_type,
    de.device_name                         AS device_name,
    de.capacity_box                        AS capacity_box,
    eq.plant_code                          AS plant_code,
    eq.run_date                            AS run_date,
    eq.run_minutes                         AS run_minutes,
    eq.stop_minutes                        AS stop_minutes,
    eq.run_minutes + eq.stop_minutes       AS total_minutes,
    eq.output_qty                          AS output_qty,
    CASE WHEN eq.run_minutes > 0
         THEN ROUND(eq.output_qty * 60 / eq.run_minutes, 2)
         ELSE 0 END                        AS hourly_output     -- 台时产量（箱/小时）
FROM ods.ods_设备运行工况 eq
LEFT JOIN dim.dim_equipment de
       ON eq.device_code = de.device_code
WHERE eq.dt = '2026-01-01';
