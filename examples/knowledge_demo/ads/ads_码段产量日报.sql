-- =============================================================
-- ADS 层：码段产量日报（按厂 / 牌号）
-- 上游：cdw.dwd_卷烟产量码段明细
-- 下游：ads.ads_经营指标驾驶舱
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_码段产量日报 PARTITION (dt = '2026-01-01')
SELECT
    d.plant_code                                          AS plant_code,        -- 生产厂编码
    d.brand_code                                          AS brand_code,        -- 牌号编码
    COUNT(1)                                              AS work_order_cnt,    -- 工单数
    SUM(d.chanliang_qty)                                  AS output_qty,        -- 产量（箱）
    ROUND(AVG(d.dama_rate), 4)                            AS avg_dama_rate,     -- 平均打码占比
    MAX(d.chanliang_cig)                                  AS max_chanliang_cig  -- 单工单最高产量（条）
FROM cdw.dwd_卷烟产量码段明细 d
WHERE d.dt = '2026-01-01'
  AND d.chanliang_qty > 0          -- 产量为 0 的工单不计入日报
GROUP BY d.plant_code, d.brand_code;
