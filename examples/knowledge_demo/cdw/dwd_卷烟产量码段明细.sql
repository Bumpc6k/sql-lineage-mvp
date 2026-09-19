-- =============================================================
-- DWD 层：码段明细 → 产量口径明细 cdw.dwd_卷烟产量码段明细
-- 上游：ods.ods_卷烟码段流水
-- 下游：ads.ads_码段产量日报 / cdw.dws_产量汇总
-- 核心口径：产量 = 打码量 + 跳码量 - 重码量（公司统一产量口径）
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_卷烟产量码段明细 PARTITION (dt = '2026-01-01')
SELECT
    b.work_order_no                                       AS work_order_no,     -- 工单号
    b.plant_code                                          AS plant_code,        -- 生产厂编码
    b.brand_code                                          AS brand_code,        -- 牌号编码
    b.batch_no                                            AS batch_no,          -- 批次号
    SUM(b.dama_qty)                                       AS dama_qty_total,    -- 打码量合计（箱）
    SUM(b.tiaoma_qty)                                     AS tiaoma_qty_total,  -- 跳码量合计（箱）
    SUM(b.chongma_qty)                                    AS chongma_qty_total, -- 重码量合计（箱）
    SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty) AS chanliang_qty,   -- 产量口径：打码量+跳码量-重码量（箱）
    CASE WHEN SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty) > 0
         THEN ROUND(SUM(b.dama_qty) / (SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty)), 6)
         ELSE 0 END                                       AS dama_rate,         -- 打码占比（打码量/产量）
    SUM(b.dama_qty) * 250                                 AS dama_cig_qty,      -- 打码量折条（箱转条）
    (SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty)) * 250 AS chanliang_cig -- 产量折条（箱转条）
FROM ods.ods_卷烟码段流水 b
WHERE b.dt = '2026-01-01'
GROUP BY b.work_order_no, b.plant_code, b.brand_code, b.batch_no;
