-- =============================================================
-- 样本 04：相关子查询 + 标量子查询 + IN (SELECT ...)
-- 特性：SELECT 列表里的标量子查询、WHERE EXISTS、WHERE IN 三处子查询
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_有效产量明细 PARTITION (dt = '2026-01-01')
SELECT
    o.work_order_no AS work_order_no,
    o.plant_code    AS plant_code,
    o.output_qty    AS output_qty,
    (SELECT MAX(s.sale_qty)
       FROM ods.ods_卷烟销量流水 s
      WHERE s.brand_code = o.brand_code
        AND s.dt = '2026-01-01')                        AS brand_max_sale_qty
FROM ods.ods_卷烟产量流水 o
WHERE o.dt = '2026-01-01'
  AND EXISTS (
        SELECT 1
          FROM dim.dim_brand b
         WHERE b.brand_code = o.brand_code
           AND b.status = '1'
      )
  AND o.brand_code IN (
        SELECT d.brand_code
          FROM dim.dim_brand d
         WHERE d.price_band = '高端'
      );
