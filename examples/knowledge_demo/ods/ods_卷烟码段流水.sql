-- =============================================================
-- ODS 层：码段采集 → 贴源表 ods.ods_卷烟码段流水
-- 上游：src.mes_码段采集接口（MES 扫码采集）
-- 下游：cdw.dwd_卷烟产量码段明细
-- 说明：一条记录 = 一次扫码事件，产量由码段口径推导
-- =============================================================
INSERT OVERWRITE TABLE ods.ods_卷烟码段流水 PARTITION (dt = '2026-01-01')
SELECT
    c.scan_id           AS scan_id,          -- 扫码流水号
    c.work_order_no     AS work_order_no,    -- 工单号
    c.plant_code        AS plant_code,       -- 生产厂编码
    c.brand_code        AS brand_code,       -- 牌号编码
    c.batch_no          AS batch_no,         -- 批次号
    c.dama_qty          AS dama_qty,         -- 打码量（箱）
    c.tiaoma_qty        AS tiaoma_qty,       -- 跳码量（箱）
    c.chongma_qty       AS chongma_qty,      -- 重码量（箱）
    c.bz                AS bz,               -- 历史遗留字段，业务含义待确认
    c.scan_time         AS scan_time         -- 扫码时间
FROM src.mes_码段采集接口 c
WHERE c.dt = '2026-01-01'
  AND c.scan_status = 'VALID'          -- 仅统计有效扫码（剔除作废扫码）
  AND c.dama_qty IS NOT NULL;          -- 打码量为空视为脏数据，直接过滤
