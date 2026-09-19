-- =============================================================
-- ADS 层：烟叶供应商排名（应用层报表）
-- 上游：cdw.dws_烟叶采购供应商汇总
-- 下游：ads.ads_经营指标驾驶舱
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_烟叶供应商排名 PARTITION (dt = '2026-01-01')
SELECT
    c.supplier_code                                 AS supplier_code,
    c.supplier_name                                 AS supplier_name,
    c.supplier_level                                AS supplier_level,
    c.purchase_cnt                                  AS purchase_cnt,
    c.total_weight                                  AS total_weight,
    c.total_amt                                     AS total_amt,
    c.avg_unit_price                                AS avg_unit_price,
    ROW_NUMBER() OVER (ORDER BY c.total_amt DESC)   AS amount_rank,
    DENSE_RANK() OVER (ORDER BY c.total_weight DESC) AS weight_rank
FROM cdw.dws_烟叶采购供应商汇总 c
WHERE c.dt = '2026-01-01';
