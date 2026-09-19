-- =============================================================
-- 示例 3：DWS -> ADS（CTE + 派生表子查询 + 多级下推）
-- 场景：卷烟产销月报，先算产量、再算销量，最后算产销率
-- 覆盖形态：WITH ... AS (...)、FROM (SELECT ...) t 子查询、
--           多层 CTE 嵌套、字段级血缘跨 CTE 下推到真实表
-- =============================================================
WITH prod AS (
    SELECT
        plant_code,
        SUM(total_output_qty) AS output_qty
    FROM dws.dws_卷烟产量汇总
    WHERE dt = '2026-01-01'
    GROUP BY plant_code
),
sale AS (
    SELECT
        plant_code,
        SUM(sale_qty) AS sale_qty,
        SUM(sale_amt) AS sale_amt
    FROM ods.ods_卷烟销量
    WHERE dt = '2026-01-01'
    GROUP BY plant_code
)
INSERT INTO TABLE ads.ads_卷烟产销月报 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code                                       AS plant_code,
    p.plant_name                                       AS plant_name,
    t.output_qty                                       AS output_qty,
    t.sale_qty                                         AS sale_qty,
    t.sale_amt                                         AS sale_amt,
    ROUND(t.sale_qty / t.output_qty, 4)                AS sale_output_ratio
FROM (
    SELECT
        pr.plant_code            AS plant_code,
        pr.output_qty            AS output_qty,
        sa.sale_qty              AS sale_qty,
        sa.sale_amt              AS sale_amt
    FROM prod pr
    JOIN sale sa ON pr.plant_code = sa.plant_code
) t
LEFT JOIN dim.dim_plant p
       ON t.plant_code = p.plant_code;
