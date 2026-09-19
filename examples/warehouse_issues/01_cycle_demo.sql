-- =============================================================
-- 问题样例 1：循环依赖（A 写 B、B 又写回 A）
-- 用来演示 `cycle` 子命令的环路检测告警
-- =============================================================
INSERT OVERWRITE TABLE dwd.dwd_销量回写池 PARTITION (dt = '2026-01-01')
SELECT
    s.brand_code      AS brand_code,
    s.corrected_qty   AS corrected_qty,
    s.dt              AS dt
FROM ads.ads_销量修正结果 s
WHERE s.dt = '2026-01-01';

INSERT OVERWRITE TABLE ads.ads_销量修正结果 PARTITION (dt = '2026-01-01')
SELECT
    p.brand_code      AS brand_code,
    p.corrected_qty   AS corrected_qty,
    p.dt              AS dt
FROM dwd.dwd_销量回写池 p
WHERE p.dt = '2026-01-01';
