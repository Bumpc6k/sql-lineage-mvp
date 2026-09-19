-- =============================================================
-- 示例 6：临时查询（无输出表的纯 SELECT，用于取数/排查）
-- 覆盖形态：task_type = SELECT（无输出表，只有输入表 + 字段级血缘）
-- 说明：SELECT * 在无元数据的情况下无法展开列，会以 resolved=false 如实标注
-- =============================================================
SELECT
    p.plant_name                    AS plant_name,
    b.brand_name                    AS brand_name,
    SUM(d.output_qty)               AS total_output_qty
FROM dwd.dwd_卷烟产量明细 d
LEFT JOIN dim.dim_plant p
       ON d.plant_code = p.plant_code
LEFT JOIN dim.dim_brand b
       ON d.brand_code = b.brand_code
WHERE d.prod_date BETWEEN '2026-01-01' AND '2026-01-31'
GROUP BY p.plant_name, b.brand_name
ORDER BY total_output_qty DESC
LIMIT 100;
