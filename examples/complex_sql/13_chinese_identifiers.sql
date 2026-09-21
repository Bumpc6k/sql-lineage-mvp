-- =============================================================
-- 样本 13：中文表名 / 中文字段名 / 含全角括号（）的别名
-- 特性：标识符含中文、全角括号、全角顿号
-- =============================================================
INSERT OVERWRITE TABLE cdw.「产量明细表」 PARTITION (dt = '2026-01-01')
SELECT
    o.work_order_no AS 工单号,
    o.plant_code    AS 厂区编码,
    o.output_qty    AS 产量（箱）,
    o.defect_qty    AS 不良品数量（箱）,
    o.shift_code    AS 班次
FROM src.生产工单明细 o
WHERE o.dt = '2026-01-01';
