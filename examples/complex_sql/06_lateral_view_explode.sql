-- =============================================================
-- 样本 06：LATERAL VIEW explode / posexplode / UDTF
-- 特性：UDTF 展开（split + explode / posexplode / explode(map)）+ 输出多列
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_工单标签明细 PARTITION (dt = '2026-01-01')
SELECT
    o.work_order_no          AS work_order_no,
    o.plant_code             AS plant_code,
    tag.t                    AS tag_name,
    pos.p                    AS tag_pos,
    CAST(mv.mv AS DOUBLE)    AS tag_value
FROM ods.ods_卷烟产量流水 o
LATERAL VIEW explode(split(o.tag_list, ',')) tag AS t
LATERAL VIEW posexplode(split(o.tag_list, ',')) pos AS p, v
LATERAL VIEW explode(map('qty', o.output_qty)) mv AS mk, mv
WHERE o.dt = '2026-01-01';
