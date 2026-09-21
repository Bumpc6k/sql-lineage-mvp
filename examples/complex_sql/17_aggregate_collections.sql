-- =============================================================
-- 样本 17：聚合集合函数（collect_list / collect_set / named_struct / map / size / concat_ws）
-- 特性：输出列是复杂类型构造，内含多列引用；配合 LATERAL VIEW OUTER
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_产量标签汇总 PARTITION (dt = '2026-01-01')
SELECT
    o.plant_code                                              AS plant_code,
    CONCAT_WS(',', COLLECT_SET(o.brand_code))                 AS brand_codes,
    COLLECT_LIST(NAMED_STRUCT('wo', o.work_order_no, 'qty', o.output_qty)) AS wo_list,
    SIZE(COLLECT_SET(o.work_order_no))                        AS wo_cnt,
    MAP('plant', o.plant_code, 'brand', MAX(o.brand_code))    AS dim_map,
    SUM(o.output_qty) / COUNT(DISTINCT o.work_order_no)       AS avg_qty_per_wo
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
GROUP BY o.plant_code;

INSERT OVERWRITE TABLE cdw.dws_标签展开 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code AS plant_code,
    tag.brand    AS brand_code
FROM (
    SELECT plant_code,
           CONCAT_WS(',', COLLECT_SET(brand_code)) AS brand_str
    FROM ods.ods_卷烟产量流水
    WHERE dt = '2026-01-01'
    GROUP BY plant_code
) t
LATERAL VIEW OUTER explode(split(t.brand_str, ',')) tag AS brand;
