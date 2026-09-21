-- =============================================================
-- 样本 05：UNION ALL 三分支（派生表内并集，外层再聚合）
-- 特性：UNION ALL 多分支 + 各分支常量列 + 外层 GROUP BY
-- =============================================================
INSERT OVERWRITE TABLE cdw.dws_产销存汇总 PARTITION (dt = '2026-01-01')
SELECT
    t.plant_code               AS plant_code,
    t.biz_type                 AS biz_type,
    SUM(t.qty)                 AS qty,
    COUNT(DISTINCT t.biz_no)    AS biz_no_cnt
FROM (
    SELECT plant_code, '产量' AS biz_type, work_order_no AS biz_no, output_qty AS qty
      FROM ods.ods_卷烟产量流水
     WHERE dt = '2026-01-01'
    UNION ALL
    SELECT plant_code, '销量' AS biz_type, outbound_no AS biz_no, sale_qty AS qty
      FROM ods.ods_卷烟销量流水
     WHERE dt = '2026-01-01'
    UNION ALL
    SELECT plant_code, '库存' AS biz_type, warehouse_code AS biz_no, stock_qty AS qty
      FROM ods.ods_成品库存快照
     WHERE dt = '2026-01-01'
) t
GROUP BY t.plant_code, t.biz_type;
