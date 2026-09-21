-- =============================================================
-- 样本 02：窗口函数（ROW_NUMBER / RANK / DENSE_RANK / SUM OVER 累计 / AVG OVER 全局）
-- 特性：OVER(PARTITION BY ... ORDER BY ... ROWS BETWEEN ...)
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_产量车间排名 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                                        AS plant_code,
    t.work_order_no                                     AS work_order_no,
    t.output_qty                                        AS output_qty,
    ROW_NUMBER() OVER (PARTITION BY t.plant_code ORDER BY t.output_qty DESC) AS rn,
    RANK()       OVER (PARTITION BY t.plant_code ORDER BY t.output_qty DESC) AS rk,
    DENSE_RANK() OVER (PARTITION BY t.plant_code ORDER BY t.output_qty DESC) AS drk,
    SUM(t.output_qty) OVER (PARTITION BY t.plant_code ORDER BY t.work_date
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_qty,
    AVG(t.output_qty) OVER (PARTITION BY t.plant_code)  AS avg_qty,
    MAX(t.output_qty) OVER (PARTITION BY t.brand_code)  AS brand_max_qty
FROM ods.ods_卷烟产量流水 t
WHERE t.dt = '2026-01-01';
