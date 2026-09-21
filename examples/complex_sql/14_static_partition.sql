-- =============================================================
-- 样本 14：静态分区插入（多分区键）+ INSERT INTO TABLE
-- 特性：PARTITION (dt = '...', area = '...') 静态值，及 INSERT INTO 追加写
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01', area = '华东')
SELECT
    o.plant_code      AS plant_code,
    SUM(o.output_qty) AS output_qty,
    MAX(o.work_date)  AS last_work_date
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
  AND o.area = '华东'
GROUP BY o.plant_code;

INSERT INTO TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-02', area = '华南')
SELECT
    o.plant_code      AS plant_code,
    SUM(o.output_qty) AS output_qty,
    MAX(o.work_date)  AS last_work_date
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-02'
  AND o.area = '华南'
GROUP BY o.plant_code;
