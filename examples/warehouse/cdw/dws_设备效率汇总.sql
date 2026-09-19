-- =============================================================
-- DWS 层：设备效率汇总
-- 上游：cdw.dwd_设备运行明细
-- 下游：ads.ads_设备运行看板
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_设备效率汇总 PARTITION (dt = '2026-01-01')
SELECT
    e.device_code                         AS device_code,
    e.device_name                         AS device_name,
    e.device_type                         AS device_type,
    e.plant_code                          AS plant_code,
    SUM(e.run_minutes)                    AS total_run_minutes,
    SUM(e.stop_minutes)                   AS total_stop_minutes,
    SUM(e.output_qty)                     AS total_output_qty,
    ROUND(AVG(e.hourly_output), 2)        AS avg_hourly_output,
    CASE WHEN SUM(e.run_minutes) + SUM(e.stop_minutes) > 0
         THEN ROUND(SUM(e.run_minutes) / (SUM(e.run_minutes) + SUM(e.stop_minutes)), 4)
         ELSE 0 END                       AS run_rate            -- 设备开动率
FROM cdw.dwd_设备运行明细 e
WHERE e.dt = '2026-01-01'
GROUP BY e.device_code, e.device_name, e.device_type, e.plant_code;
