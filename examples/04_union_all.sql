-- =============================================================
-- 示例 4：UNION ALL 合并多来源（自产 + 外采，或 省内 + 省外）
-- 场景：把「自产卷烟」与「外购卷烟」两个来源统一进 ADS 口径表
-- 覆盖形态：UNION ALL 多分支（字段按位置对齐）、各分支独立过滤条件
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_卷烟货源汇总 PARTITION (dt = '2026-01-01')
SELECT
    src_type,
    plant_code,
    brand_code,
    SUM(qty) AS qty
FROM (
    SELECT
        '自产'          AS src_type,
        a.plant_code    AS plant_code,
        a.brand_code    AS brand_code,
        a.output_qty    AS qty
    FROM dwd.dwd_卷烟产量明细 a
    WHERE a.prod_date = '2026-01-01'

    UNION ALL

    SELECT
        '外购'          AS src_type,
        b.plant_code    AS plant_code,
        b.brand_code    AS brand_code,
        b.purchase_qty  AS qty
    FROM ods.ods_卷烟外购入库 b
    WHERE b.dt = '2026-01-01'
      AND b.purchase_qty > 0
) u
GROUP BY src_type, plant_code, brand_code;
