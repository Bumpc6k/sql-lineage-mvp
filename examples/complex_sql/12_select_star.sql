-- =============================================================
-- 样本 12：SELECT *（无法展开列，已知限制）
-- 特性：裸 SELECT *、限定 SELECT a.*、以及 * 混在表达式里
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_产量备份 PARTITION (dt = '2026-01-01')
SELECT * FROM ods.ods_卷烟产量流水 WHERE dt = '2026-01-01';

INSERT OVERWRITE TABLE cdw.dwd_产量备份2 PARTITION (dt = '2026-01-01')
SELECT
    p.*,
    p.output_qty * 250 AS output_qty_cig
FROM ods.ods_卷烟产量流水 p
WHERE p.dt = '2026-01-01';
