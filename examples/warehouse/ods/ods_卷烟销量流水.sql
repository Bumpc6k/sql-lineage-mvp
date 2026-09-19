-- =============================================================
-- ODS 层：销售出库 → 贴源表 ods.ods_卷烟销量流水
-- 上游：src.mes_销售出库明细
-- 下游：cdw.dwd_卷烟销量明细
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_卷烟销量流水 PARTITION (dt = '2026-01-01')
SELECT
    b.outbound_no       AS outbound_no,        -- 出库单号
    b.customer_code     AS customer_code,      -- 客户编码
    b.brand_code        AS brand_code,         -- 牌号编码
    b.sale_date         AS sale_date,          -- 销售日期
    CAST(b.sale_qty AS DECIMAL(18, 4))         AS sale_qty,       -- 销量（箱）
    CAST(b.sale_amt AS DECIMAL(18, 2))         AS sale_amt,       -- 销售额（元）
    b.channel_code      AS channel_code,       -- 渠道编码
    b.update_time       AS update_time
FROM src.mes_销售出库明细 b
WHERE b.dt = '2026-01-01'
  AND b.sale_qty > 0;
