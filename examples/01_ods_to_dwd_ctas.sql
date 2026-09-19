-- =============================================================
-- 示例 1：ODS -> DWD（CTAS 建表 + 单表清洗 + 分区过滤）
-- 场景：把卷烟产量 ODS 流水表清洗成 DWD 明细表，补上统一的厂/牌号编码
-- 覆盖形态：CREATE TABLE ... AS SELECT、WHERE 分区过滤、字段重命名
-- =============================================================
CREATE TABLE IF NOT EXISTS dwd.dwd_卷烟产量明细 AS
SELECT
    a.plant_code            AS plant_code,        -- 厂编码
    a.brand_code            AS brand_code,        -- 牌号编码
    a.prod_date             AS prod_date,         -- 生产日期
    a.output_qty            AS output_qty,        -- 产量（箱）
    a.output_qty * 250      AS output_qty_cig,    -- 产量换算成条
    a.update_time           AS etl_time
FROM ods.ods_卷烟产量 a
WHERE a.dt = '2026-01-01'
  AND a.output_qty > 0;
