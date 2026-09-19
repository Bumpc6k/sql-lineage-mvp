-- =============================================================
-- ODS 层：IoT 采集 → 贴源表 ods.ods_设备运行工况
-- 上游：src.iot_设备工况采集
-- 下游：cdw.dwd_设备运行明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_设备运行工况 PARTITION (dt = '2026-01-01')
SELECT
    f.device_code       AS device_code,        -- 设备编码
    f.device_type       AS device_type,        -- 设备类型（卷包机组/制丝线）
    f.plant_code        AS plant_code,         -- 所属厂
    f.run_date          AS run_date,           -- 运行日期
    CAST(f.run_minutes AS BIGINT)              AS run_minutes,     -- 运行时长（分钟）
    CAST(f.stop_minutes AS BIGINT)             AS stop_minutes,    -- 停机时长（分钟）
    CAST(f.output_qty AS DECIMAL(18, 4))       AS output_qty,      -- 台时产量（箱）
    f.update_time       AS update_time
FROM src.iot_设备工况采集 f
WHERE f.dt = '2026-01-01'
  AND f.run_date >= '2026-01-01';
