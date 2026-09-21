-- =============================================================
-- 样本 11：复杂字段表达式（CASE WHEN / COALESCE / CAST / NULLIF / 字符串函数 / 嵌套聚合 / 窗口）
-- 特性：几乎每一列都是一个非平凡表达式
-- =============================================================
INSERT OVERWRITE TABLE cdw.dwd_产量质量明细 PARTITION (dt = '2026-01-01')
SELECT
    p.work_order_no                                              AS work_order_no,
    COALESCE(p.plant_code, 'UNKNOWN')                            AS plant_code,
    CAST(p.output_qty AS DECIMAL(18, 2))                         AS output_qty_dec,
    ROUND(p.defect_qty / NULLIF(p.output_qty, 0), 6)             AS defect_rate,
    CASE WHEN p.output_qty > 10000 THEN '大批量'
         WHEN p.output_qty > 1000 AND p.shift_code = 'A' THEN '中批量'
         ELSE '小批量' END                                       AS batch_level,
    CONCAT_WS('-', p.plant_code, p.brand_code, SUBSTR(p.work_date, 1, 10)) AS biz_key,
    NVL(p.brand_code, 'NA')                                      AS brand_code,
    SUM(p.output_qty) OVER (PARTITION BY p.plant_code)           AS plant_total_qty,
    MAX(CASE WHEN p.defect_qty > 0 THEN 1 ELSE 0 END)            AS has_defect_flag,
    COUNT(1)                                                     AS row_cnt,
    UPPER(TRIM(p.shift_code))                                    AS shift_code_clean
FROM ods.ods_卷烟产量流水 p
WHERE p.dt = '2026-01-01';
