-- =============================================================
-- 样本 19：MERGE INTO（Hive 3 / ACID 增量合并）
-- 特性：MERGE INTO ... USING ... ON ... WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...
-- =============================================================
MERGE INTO cdw.dwd_产量明细 t
USING (
    SELECT o.work_order_no AS work_order_no,
           o.plant_code    AS plant_code,
           o.output_qty    AS output_qty
    FROM ods.ods_卷烟产量流水 o
    WHERE o.dt = '2026-01-01'
) s
ON t.work_order_no = s.work_order_no
WHEN MATCHED THEN
    UPDATE SET t.output_qty = s.output_qty
WHEN NOT MATCHED THEN
    INSERT (work_order_no, plant_code, output_qty)
    VALUES (s.work_order_no, s.plant_code, s.output_qty);
