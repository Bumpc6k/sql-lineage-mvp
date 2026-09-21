-- =============================================================
-- 样本 10：建表语句（CREATE TABLE ... LIKE ...）与带完整列定义的建表
-- 特性：LIKE 复制表结构 / 显式列定义建表（无 SELECT，不产生血缘）
-- =============================================================
CREATE TABLE IF NOT EXISTS cdw.dwd_产量明细_归档
LIKE cdw.dwd_卷烟产量明细
STORED AS ORC
TBLPROPERTIES ('comment' = '产量明细归档表');

CREATE TABLE IF NOT EXISTS cdw.dim_班次 (
    shift_code   STRING  COMMENT '班次编码',
    shift_name   STRING  COMMENT '班次名称',
    start_hour   INT     COMMENT '开始小时'
)
COMMENT '班次维表'
STORED AS ORC;
