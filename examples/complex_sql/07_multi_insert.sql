-- =============================================================
-- 样本 07：Hive 多插入（FROM src ... INSERT OVERWRITE a SELECT ... INSERT OVERWRITE b SELECT ...）
-- 特性：一条语句多个输出表，扫描源表只写一次
-- =============================================================
FROM ods.ods_卷烟产量流水 o
INSERT OVERWRITE TABLE cdw.dwd_产量明细 PARTITION (dt = '2026-01-01')
SELECT o.work_order_no AS work_order_no,
       o.plant_code    AS plant_code,
       o.output_qty    AS output_qty
WHERE o.dt = '2026-01-01'
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT o.plant_code  AS plant_code,
       SUM(o.output_qty) AS output_qty
WHERE o.dt = '2026-01-01'
GROUP BY o.plant_code
INSERT OVERWRITE TABLE cdw.dws_品牌产量汇总 PARTITION (dt = '2026-01-01')
SELECT o.brand_code  AS brand_code,
       SUM(o.output_qty) AS output_qty
WHERE o.dt = '2026-01-01'
GROUP BY o.brand_code;
