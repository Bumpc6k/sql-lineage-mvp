-- 来源：wf_ods_采集 / t_ods_销量流水（SQL），抽取自 taskParams.sql
-- ODS 贴源：MES 销售出库 -> ods.ods_卷烟销量流水
INSERT OVERWRITE TABLE ods.ods_卷烟销量流水 PARTITION (dt = '2026-01-01')
SELECT
    b.outbound_no                          AS outbound_no,
    b.customer_code                        AS customer_code,
    b.brand_code                           AS brand_code,
    b.sale_date                            AS sale_date,
    CAST(b.sale_qty AS DECIMAL(18, 4))     AS sale_qty,
    CAST(b.sale_amt AS DECIMAL(18, 2))     AS sale_amt,
    b.channel_code                         AS channel_code,
    b.update_time                          AS update_time
FROM src.mes_销售出库明细 b
WHERE b.dt = '2026-01-01'
  AND b.sale_qty > 0;
