-- =============================================================
-- 样本 09：CTAS + COMMENT + STORED AS + TBLPROPERTIES
-- 特性：CREATE TABLE ... COMMENT ... STORED AS ORC ... TBLPROPERTIES (...) AS SELECT
-- =============================================================
CREATE TABLE IF NOT EXISTS cdw.dwd_产量明细_ctas
COMMENT '卷烟产量明细（CTAS 建表）'
STORED AS ORC
TBLPROPERTIES ('orc.compress' = 'SNAPPY', 'lifecycle' = '365')
AS
SELECT
    p.work_order_no   AS work_order_no,
    p.plant_code      AS plant_code,
    p.brand_code      AS brand_code,
    p.output_qty      AS output_qty,
    p.shift_code      AS shift_code
FROM ods.ods_卷烟产量流水 p
WHERE p.dt = '2026-01-01';
