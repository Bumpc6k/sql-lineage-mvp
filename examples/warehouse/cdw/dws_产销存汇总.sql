-- =============================================================
-- DWS 层：产销存汇总（同一个调度节点里的多段 SQL，3 条语句）
-- 上游：cdw.dwd_卷烟产量明细 / cdw.dwd_成品库存明细 / cdw.dwd_卷烟销量明细
-- 下游：ads.ads_产销存月报 / ads.ads_税利分析
-- =============================================================

-- [1/3] 产量汇总
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                       AS plant_code,
    d.brand_code                       AS brand_code,
    SUM(d.output_qty)                  AS total_output_qty,
    SUM(d.defect_qty)                  AS total_defect_qty,
    COUNT(1)                           AS work_order_cnt
FROM cdw.dwd_卷烟产量明细 d
WHERE d.dt = '2026-01-01'
GROUP BY d.plant_code, d.brand_code;

-- [2/3] 库存汇总
INSERT OVERWRITE TABLE cdw.dws_库存汇总 PARTITION (dt = '2026-01-01')
SELECT
    k.plant_code                       AS plant_code,
    k.brand_code                       AS brand_code,
    SUM(k.stock_qty)                   AS total_stock_qty,
    SUM(k.stock_amt)                   AS total_stock_amt,
    COUNT(DISTINCT k.batch_no)         AS batch_cnt
FROM cdw.dwd_成品库存明细 k
WHERE k.dt = '2026-01-01'
GROUP BY k.plant_code, k.brand_code;

-- [3/3] 产销存合并汇总（产量汇总 + 库存汇总 + 销量明细）
INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')
SELECT
    a.plant_code                       AS plant_code,
    a.brand_code                       AS brand_code,
    a.total_output_qty                 AS output_qty,
    COALESCE(s.total_sale_qty, 0)      AS sale_qty,
    COALESCE(k.total_stock_qty, 0)     AS stock_qty,
    COALESCE(s.total_sale_amt, 0)      AS sale_amt
FROM cdw.dws_产量汇总 a
LEFT JOIN (
    SELECT
        d.plant_code     AS plant_code,
        d.brand_code     AS brand_code,
        SUM(d.sale_qty)  AS total_sale_qty,
        SUM(d.sale_amt)  AS total_sale_amt
    FROM cdw.dwd_卷烟销量明细 d
    WHERE d.dt = '2026-01-01'
    GROUP BY d.plant_code, d.brand_code
) s
  ON a.plant_code = s.plant_code AND a.brand_code = s.brand_code
LEFT JOIN cdw.dws_库存汇总 k
  ON a.plant_code = k.plant_code AND a.brand_code = k.brand_code
WHERE a.dt = '2026-01-01';
