-- 来源：wf_dwd_清洗 / t_dwd_销量明细（SQL），抽取自 taskParams.sql
-- DWD 明细：销量贴源 + 牌号维表 -> cdw.dwd_卷烟销量明细
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
         ELSE 0 END                        AS unit_price
FROM ods.ods_卷烟销量流水 s
LEFT JOIN dim.dim_brand bd
       ON s.brand_code = bd.brand_code
WHERE s.dt = '2026-01-01';
