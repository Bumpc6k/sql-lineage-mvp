-- =============================================================
-- ADS 层：设备运行看板（应用层报表）
-- 上游：cdw.dws_设备效率汇总
-- 下游：ads.ads_经营指标驾驶舱
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_设备运行看板 PARTITION (dt = '2026-01-01')
SELECT
    e.plant_code                                    AS plant_code,
    e.device_type                                   AS device_type,
    COUNT(1)                                        AS device_cnt,
    SUM(e.total_run_minutes)                        AS run_minutes,
    SUM(e.total_stop_minutes)                       AS stop_minutes,
    SUM(e.total_output_qty)                         AS output_qty,
    ROUND(AVG(e.run_rate), 4)                       AS avg_run_rate,
    SUM(CASE WHEN e.run_rate < 0.6 THEN 1 ELSE 0 END) AS low_run_rate_device_cnt
FROM cdw.dws_设备效率汇总 e
WHERE e.dt = '2026-01-01'
GROUP BY e.plant_code, e.device_type;
