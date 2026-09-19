-- =============================================================
-- DWD 层：贴源销量 → 明细事实 dwd.dwd_卷烟销量明细
-- 上游：ods.ods_卷烟销量流水 + dim.dim_brand
-- 下游：cdw.dws_产销存汇总
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_卷烟销量明细 PARTITION (dt = '2026-01-01')
SELECT
    s.outbound_no                          AS outbound_no,
    s.sale_date                            AS sale_date,
    s.customer_code                        AS customer_code,
    s.brand_code                           AS brand_code,
    bd.brand_name                          AS brand_name,
    s.channel_code                         AS channel_code,
    s.sale_qty                             AS sale_qty,
    s.sale_amt                             AS sale_amt,
    CASE WHEN s.sale_qty > 0
         THEN ROUND(s.sale_amt / s.sale_qty, 2)
         ELSE 0 END                        AS unit_price        -- 箱单价
FROM ods.ods_卷烟销量流水 s
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
