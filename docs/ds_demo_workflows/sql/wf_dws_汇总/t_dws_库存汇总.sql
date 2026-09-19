-- 来源：wf_dws_汇总 / t_dws_库存汇总（SQL），抽取自 taskParams.sql
-- DWS 汇总：按厂 / 牌号聚合库存
INSERT OVERWRITE TABLE cdw.dws_库存汇总 PARTITION (dt = '2026-01-01')
SELECT
    k.plant_code                           AS plant_code,
    k.brand_code                           AS brand_code,
    SUM(k.stock_qty)                       AS total_stock_qty,
    SUM(k.stock_amt)                       AS total_stock_amt,
    COUNT(DISTINCT k.batch_no)             AS batch_cnt
FROM cdw.dwd_成品库存明细 k
WHERE k.dt = '2026-01-01'
GROUP BY k.plant_code, k.brand_code;
