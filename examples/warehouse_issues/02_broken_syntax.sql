-- =============================================================
-- 问题样例 2：语法错误（故意写坏）——演示扫描报告里的「解析失败清单」
-- 下面这条 SELECT 缺了 FROM 的完整表名，且括号不闭合
-- =============================================================
INSERT OVERWRITE TABLE ads.ads_坏掉的表 PARTITION (dt = '2026-01-01')
SELECT
    x.brand_code AS brand_code,
    SUM(x.qty AS total_qty
FROM ods.ods_不存在表 x
WHERE x.dt = '2026-01-01
