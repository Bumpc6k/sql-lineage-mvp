-- =============================================================
-- 样本 08：动态分区插入（PARTITION (dt) —— 分区值来自 SELECT 最后一列）
-- 特性：SET 配置语句 + 动态分区
-- =============================================================
SET hive.exec.dynamic.partition = true;
SET hive.exec.dynamic.partition.mode = nonstrict;
SET hive.exec.max.dynamic.partitions = 3000;

INSERT OVERWRITE TABLE cdw.dws_产量日汇总 PARTITION (dt)
SELECT
    o.plant_code        AS plant_code,
    SUM(o.output_qty)   AS output_qty,
    COUNT(1)            AS wo_cnt,
    o.work_date         AS dt
FROM ods.ods_卷烟产量流水 o
GROUP BY o.plant_code, o.work_date;
