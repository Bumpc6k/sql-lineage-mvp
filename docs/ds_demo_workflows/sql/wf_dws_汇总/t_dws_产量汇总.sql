-- 来源：wf_dws_汇总 / t_dws_产量汇总（SQL），抽取自 taskParams.sql
-- DWS 汇总：按厂 / 牌号聚合产量
INSERT OVERWRITE TABLE cdw.dws_产量汇总 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                           AS plant_code,
    d.brand_code                           AS brand_code,
    SUM(d.output_qty)                      AS total_output_qty,
    SUM(d.defect_qty)                      AS total_defect_qty,
    COUNT(1)                               AS work_order_cnt
FROM cdw.dwd_卷烟产量明细 d
WHERE d.dt = '2026-01-01'
GROUP BY d.plant_code, d.brand_code;
