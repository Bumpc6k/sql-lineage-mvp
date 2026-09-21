-- =============================================================
-- 样本 18：多层嵌套派生表（三层）+ LAG / LEAD 窗口
-- 特性：最内层过滤 → 中层聚合 → 外层窗口函数，派生表层层嵌套
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_产量环比 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                                                          AS plant_code,
    t.work_date                                                           AS work_date,
    t.output_qty                                                          AS output_qty,
    LAG(t.output_qty, 1)  OVER (PARTITION BY t.plant_code ORDER BY t.work_date) AS prev_qty,
    LEAD(t.output_qty, 1, 0) OVER (PARTITION BY t.plant_code ORDER BY t.work_date) AS next_qty,
    t.output_qty - LAG(t.output_qty, 1) OVER (PARTITION BY t.plant_code ORDER BY t.work_date) AS diff_qty
FROM (
    SELECT d.plant_code      AS plant_code,
           d.work_date       AS work_date,
           SUM(d.output_qty) AS output_qty
    FROM (
        SELECT o.plant_code AS plant_code,
               o.work_date  AS work_date,
               o.output_qty AS output_qty
        FROM ods.ods_卷烟产量流水 o
        WHERE o.dt <= '2026-01-01'
    ) d
    GROUP BY d.plant_code, d.work_date
) t;
