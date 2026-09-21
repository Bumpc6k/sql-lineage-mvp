# 复杂 SQL 解析能力矩阵（实测）

> **本文所有结论都来自真实运行结果，不是估算。**
> 样本：`examples/complex_sql/`（19 个真实风格复杂 Hive SQL，落盘为固定回归样本）
> 探针：`demos/complex_sql_probe.py`（逐个样本跑 `SqlLineageParser` 并落 JSON 证据）
> 证据：`reports/complex_sql_probe.json` + `reports/complex_sql_probe.txt`
> 环境：Python 3.11.15 / sqlglot 30.18.0 / `dialect='hive'`

复现命令：

```bash
# 全量实测（终端摘要 + 落 JSON）
env -u http_proxy -u https_proxy .venv/bin/python demos/complex_sql_probe.py

# 只看某几个样本 + 打印字段级血缘明细
env -u http_proxy -u https_proxy .venv/bin/python demos/complex_sql_probe.py 04 06 12 --detail 04 06 12
```

---

## 0. 状态判定标准

| 状态 | 含义 |
| --- | --- |
| ✅ 支持 | 表级血缘 + 字段级血缘都**完整正确**，不需要人工兜底 |
| ⚠️ 部分 | 不报错，但血缘**有缺失或有偏差**（漏表 / 漏字段 / 输出 `resolved=false` / 给出错误的「已解析」结论） |
| ❌ 不支持 | 解析直接失败（整条语句血缘全丢），或识别成 `OTHER` 完全不出血缘 |

> 字段级血缘的「resolved 比例」= `column_lineage` 里 `resolved=true` 的条数 / 总条数。
> **注意**：`resolved=true` 不等于「结论正确」——见样本 04 的标量子查询被误判为常量。

---

## 1. 结论速览

**19 个样本：✅ 12 个 / ⚠️ 5 个 / ❌ 2 个。**

| # | 样本 | 特性 | 表级血缘 | 字段级血缘 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 01 | `01_multi_cte.sql` | 多层 CTE（`b` 引用 `a`） | ✅ 2 输入 / 1 输出，2 对 | ✅ 4 条 / resolved 4（100%） | ✅ |
| 02 | `02_window_functions.sql` | ROW_NUMBER / RANK / DENSE_RANK / SUM OVER 累计 / AVG OVER 全局 | ✅ 1 / 1，1 对 | ✅ 16 条 / resolved 16（100%） | ✅ |
| 03 | `03_four_table_join.sql` | 5 表 JOIN（LEFT / INNER / FULL OUTER / LEFT） | ✅ 5 / 1，5 对 | ✅ 7 条 / resolved 7（100%） | ✅ |
| 04 | `04_correlated_subquery.sql` | 标量子查询 + `WHERE EXISTS` + `WHERE IN (SELECT)` | ⚠️ 只有 1 张输入表，**漏了 `dim.dim_brand`**（EXISTS + IN 里的表） | ⚠️ 4 条全 resolved，但 `brand_max_sale_qty` 被标成 `(常量)`（**错误结论**） | ⚠️ |
| 05 | `05_union_all.sql` | UNION ALL 三分支 + 外层聚合 | ✅ 3 / 1，3 对 | ✅ 10 条 / resolved 10（100%） | ✅ |
| 06 | `06_lateral_view_explode.sql` | `LATERAL VIEW explode / posexplode / explode(map)` | ✅ 1 / 1，1 对 | ⚠️ 5 条 / resolved 2（40%），3 个 UDTF 输出列 unresolved | ⚠️ |
| 07 | `07_multi_insert.sql` | Hive 多插入（`FROM src` + 3 个 `INSERT ... SELECT`） | ❌ 解析异常 | ❌ 解析异常 | ❌ |
| 08 | `08_dynamic_partition.sql` | `SET ...` ×3 + 动态分区 `PARTITION (dt)` | ✅ 1 / 1，1 对（3 条 `SET` 记为 `OTHER`，不影响） | ✅ 4 条 / resolved 4（100%） | ✅ |
| 09 | `09_ctas_comment_tblproperties.sql` | CTAS + `COMMENT` + `STORED AS ORC` + `TBLPROPERTIES` | ✅ 1 / 1，1 对 | ✅ 5 条 / resolved 5（100%） | ✅ |
| 10 | `10_create_table_like_def.sql` | `CREATE TABLE ... LIKE ...` + 显式列定义建表 | ❌ 两个 `CREATE` 都是 `OTHER`，**目标表完全没识别**（0 输出表） | ❌ 无 | ⚠️ |
| 11 | `11_complex_expressions.sql` | CASE WHEN / COALESCE / CAST / NULLIF / 字符串函数 / 嵌套聚合 / 窗口 | ✅ 1 / 1，1 对 | ✅ 16 条 / resolved 16（100%） | ✅ |
| 12 | `12_select_star.sql` | `SELECT *` / `SELECT p.*` + 表达式 | ✅ 1 / 2，2 对 | ⚠️ 3 条 / resolved 1（33%），`*` 如实记为 unresolved（**不报错**） | ⚠️ |
| 13 | `13_chinese_identifiers.sql` | 中文表名 / 中文字段名 / 全角括号别名 | ✅ 1 / 1，1 对 | ✅ 5 条 / resolved 5（100%） | ✅ |
| 14 | `14_static_partition.sql` | 静态多分区 `PARTITION (dt, area)` + `INSERT INTO TABLE` | ✅ 1 / 1，1 对；分区值 `{dt: 2026-01-01, area: 华东}` 正确 | ✅ 6 条 / resolved 6（100%） | ✅ |
| 15 | `15_grouping_sets.sql` | `GROUPING SETS` / `ROLLUP` / `CUBE` | ✅ 1 / 3，3 对 | ✅ 10 条 / resolved 10（100%） | ✅ |
| 16 | `16_semi_anti_join.sql` | `LEFT SEMI JOIN` / `LEFT ANTI JOIN` + `DISTINCT` | ✅ 3 / 1，3 对 | ✅ 3 条 / resolved 3（100%） | ✅ |
| 17 | `17_aggregate_collections.sql` | `collect_list/set` / `named_struct` / `map` / `size` / `concat_ws` + `LATERAL VIEW OUTER` | ✅ 1 / 2，2 对 | ⚠️ 11 条 / resolved 10（91%），`LATERAL VIEW OUTER` 输出列 unresolved | ⚠️ |
| 18 | `18_nested_subquery_window.sql` | 三层嵌套派生表 + `LAG` / `LEAD` | ✅ 1 / 1，1 对 | ✅ 12 条 / resolved 12（100%） | ✅ |
| 19 | `19_merge_into.sql` | `MERGE INTO ... USING ... WHEN MATCHED/NOT MATCHED` | ❌ `task_type=OTHER`，完全无血缘 | ❌ 无 | ❌ |

---

## 2. 逐样本实测明细

### 样本 01：多层 CTE ✅

```text
[01_multi_cte.sql]  (1036 字符)
  语句数=1
  输入表: ods.ods_卷烟产量流水, dim.dim_plant
  输出表: cdw.dws_产量汇总
  字段级血缘: 4 条 (resolved=4, unresolved=0, 比例=1.0)
    - stmt#1 INSERT_SELECT: 2 条表级 / 4 条字段级 (resolved 4)
```

`WITH base AS (...) , agg AS (... base ...), jnd AS (... agg ... LEFT JOIN dim.dim_plant)` 三层链式引用 +
`INSERT OVERWRITE ... SELECT ... FROM jnd`：CTE 名没有出现在输入表里，血缘正确下推到 `ods.ods_卷烟产量流水`
与 `dim.dim_plant`，字段级 4/4。**无问题。**

### 样本 04：相关子查询 ⚠️（问题样本）

```text
[04_correlated_subquery.sql]  (899 字符)
  语句数=1
  输入表: ods.ods_卷烟产量流水          ← 漏了 dim.dim_brand、ods.ods_卷烟销量流水
  输出表: ads.ads_有效产量明细
  字段级血缘: 4 条 (resolved=4, unresolved=0, 比例=1.0)
```

字段级明细（`--detail`）：

```text
    ads.ads_有效产量明细.work_order_no  <-  ods.ods_卷烟产量流水.work_order_no   [o.work_order_no AS work_order_no]
    ads.ads_有效产量明细.plant_code  <-  ods.ods_卷烟产量流水.plant_code   [o.plant_code AS plant_code]
    ads.ads_有效产量明细.output_qty  <-  ods.ods_卷烟产量流水.output_qty   [o.output_qty AS output_qty]
    ads.ads_有效产量明细.brand_max_sale_qty  <-  (常量)   [(SELECT MAX(s.sale_qty) FROM ods.ods_卷烟销量流水 AS s WHERE s.brand_code = o.brand_code AND s.dt = '2026-01-01') AS brand_max_sale_qty]
```

两个真实缺陷：

1. **表级血缘漏表**：`WHERE EXISTS (SELECT 1 FROM dim.dim_brand ...)` 与
   `o.brand_code IN (SELECT d.brand_code FROM dim.dim_brand d ...)` 里的 `dim.dim_brand`，
   以及标量子查询里的 `ods.ods_卷烟销量流水`，都**没有**进 `input_tables`。
   影响：下游影响分析会漏掉这两张表（改 `dim.dim_brand` 不会提示影响本任务）。
2. **标量子查询被误判为「常量」（比「未解析」更危险）**：`(SELECT MAX(s.sale_qty) ...)` 的输出列
   被记成 `source_column='(常量)'` 且 `resolved=true`。这不是「不知道」，而是**给了一个错误结论**
   —— 该字段明明来自 `ods.ods_卷烟销量流水.sale_qty`。

### 样本 06：LATERAL VIEW explode ⚠️

```text
[06_lateral_view_explode.sql]  (759 字符)
  输入表: ods.ods_卷烟产量流水      输出表: cdw.dwd_工单标签明细
  字段级血缘: 5 条 (resolved=2, unresolved=3, 比例=0.4)
    - stmt#1 INSERT_SELECT: 1 条表级 / 5 条字段级 (resolved 2)
        未解析列: cdw.dwd_工单标签明细.tag_name, cdw.dwd_工单标签明细.tag_pos, cdw.dwd_工单标签明细.tag_value
```

表级血缘正确（1 输入 1 输出）；UDTF 产出的 3 列（`tag.t` / `pos.p` / `mv.mv`）如实记为
`resolved=false`，**没有编造来源**——这是「宁少勿假」的正确行为，但不是完整能力。

### 样本 07：Hive 多插入 ❌（最严重）

```text
[07_multi_insert.sql]  (858 字符)
  !! 解析异常: ValueError: SQL 解析失败（dialect=hive）：Invalid expression / Unexpected token. Line 6, Col: 6.
```

`FROM src ... INSERT OVERWRITE TABLE a SELECT ... INSERT OVERWRITE TABLE b SELECT ...` 是 Hive 常用的
「一次扫描多个输出」写法。sqlglot 把它当「以 FROM 开头的语句」，`FROM` 后遇到 `INSERT` 直接报
`Unexpected token`。**实测 hive / spark / spark2 / databricks / presto / trino 六个方言全部失败**：

```python
>>> for d in ("hive","spark","spark2","databricks","presto","trino"):
...     sqlglot.parse("FROM t INSERT OVERWRITE TABLE a SELECT x", read=d)
所有方言：ParseError: Invalid expression / Unexpected token. Line 2, Col: 6.
```

后果：`lineage/workflow.py` 的工作流级分析里，这类任务的**整段脚本血缘全丢**
（只落一条 `errors`），后续断链/孤岛体检会误报。

### 样本 10：建表语句（LIKE / 列定义）⚠️

```text
[10_create_table_like_def.sql]  (535 字符)
  语句数=2
  输入表: (无)
  输出表: (无)              ← 目标表 cdw.dwd_产量明细_归档 / cdw.dim_班次 完全没识别
  字段级血缘: 0 条
    - stmt#1 OTHER: 0 条表级 / 0 条字段级 (resolved 0)
    - stmt#2 OTHER: 0 条表级 / 0 条字段级 (resolved 0)
```

sqlglot 其实**解析成功了**，`CREATE TABLE a LIKE b` 的 AST 里 `this=Table(a)`、
`properties` 里有 `LikeProperty LIKE b`（可用 `c.find_all(exp.Table)` 拿到 `a`、`b` 两张表）。
问题出在 `lineage/parser.py::_classify`：只要 `CREATE` 的 `body` 不是查询就一律返回 `OTHER`，
于是目标表也没了。`CREATE TABLE ... LIKE ...` 在数仓里是常见的「建归档表/影子表」写法，
目标表丢失会让图里凭空少一个节点。

### 样本 12：SELECT * ⚠️（符合预期，已验证行为）

```text
[12_select_star.sql]  (497 字符)
  输入表: ods.ods_卷烟产量流水
  输出表: cdw.dwd_产量备份, cdw.dwd_产量备份2
  字段级血缘: 3 条 (resolved=1, unresolved=2, 比例=0.3333)
    - stmt#1 INSERT_SELECT: 1 条表级 / 1 条字段级 (resolved 0)
        未解析列: cdw.dwd_产量备份.*
    - stmt#2 INSERT_SELECT: 1 条表级 / 2 条字段级 (resolved 1)
        未解析列: cdw.dwd_产量备份2.*
```

字段级明细：

```text
    cdw.dwd_产量备份.*  <-  ods.ods_卷烟产量流水.*   [*]  <<UNRESOLVED>>
    cdw.dwd_产量备份2.*  <-  ods.ods_卷烟产量流水.*   [p.*]  <<UNRESOLVED>>
    cdw.dwd_产量备份2.output_qty_cig  <-  ods.ods_卷烟产量流水.output_qty   [p.output_qty * 250 AS output_qty_cig]
```

**确认行为：不报错、表级血缘正确、`*` 明确标 `resolved=false`、非 `*` 的列照常解析。**
这是文档里声明的已知限制（无元数据无法展开），行为符合预期。

### 样本 17：聚合集合函数 + LATERAL VIEW OUTER ⚠️

```text
[17_aggregate_collections.sql]  (1252 字符)
  字段级血缘: 11 条 (resolved=10, unresolved=1, 比例=0.9091)
    - stmt#1 INSERT_SELECT: 1 条表级 / 9 条字段级 (resolved 9)
    - stmt#2 INSERT_SELECT: 1 条表级 / 2 条字段级 (resolved 1)
        未解析列: cdw.dws_标签展开.brand_code
```

`COLLECT_LIST(NAMED_STRUCT('wo', o.work_order_no, 'qty', o.output_qty))` 正确展开了**两个**来源列
（9 条字段级里包含 `wo_list <- work_order_no` 和 `wo_list <- output_qty`），`MAP(...)`、
`SIZE(...)`、`SUM/COUNT` 嵌套也都解析正确。唯一缺失：`LATERAL VIEW OUTER explode(...)` 的
输出列 `tag.brand` 无法回溯到内层派生表。
另外 sqlglot 对 `NAMED_STRUCT` 会往 stderr 打一行 `Hive does not support named structs.`——
**是 warning 不是错误**，解析结果不受影响（探针里能看到 4 行这种提示）。

### 样本 19：MERGE INTO ❌

```text
[19_merge_into.sql]  (721 字符)
  语句数=1
  输入表: (无)     输出表: (无)     字段级血缘: 0 条
    - stmt#1 OTHER: 0 条表级 / 0 条字段级 (resolved 0)
```

sqlglot **能解析** MERGE（AST 类型 `exp.Merge`），实测属性可用：
`m.this = cdw.dwd_产量明细 AS t`（目标表）、`m.args['using'] = (SELECT ... FROM ods.ods_卷烟产量流水) AS s`、
`m.args['on'] = t.work_order_no = s.work_order_no`。问题是 `_classify` 没有 `exp.Merge` 分支，
直接落到 `TASK_OTHER`。**这是纯分类缺口，不是语法能力缺口**，补齐成本很低。

---

## 3. 问题清单与可执行改进建议

按「收益/成本」排序。所有建议都基于上面的实测 AST 结构，不是猜的。

### P0-1 多插入语句解析失败（样本 07）—— 建议：解析前做一次「多插入展开」

* **问题定位**：`lineage/parser.py::parse_sql` 直接把整段文本交给 `sqlglot.parse`，多插入语法
  sqlglot 所有方言都不支持。
* **建议做法**（纯文本预处理，不改解析器核心）：
  1. 在 `parse_sql` 前加一道 `_expand_multi_insert(sql)`：用 sqlglot 的 tokenizer
     （或简单括号深度扫描）找到**深度为 0** 的 `INSERT` 关键字序列；
  2. 若文本以 `FROM ...`（或 `FROM ... JOIN ...`）开头且含 ≥2 个顶层 `INSERT`：
     抽出共享的 `FROM/JOIN` 片段 `S`，把文本改写成
     `INSERT ... SELECT ...` + `FROM S`（每条插入一份），得到 N 条独立语句；
  3. 逐条走原有 `analyze_statement`，血缘结果按语句合并即可。
* **验收**：`examples/complex_sql/07_multi_insert.sql` 应产出 3 对表级血缘
  （`ods.ods_卷烟产量流水 → cdw.dwd_产量明细 / cdw.dws_产量汇总 / cdw.dws_品牌产量汇总`）。
* **兜底**（若不改解析器）：至少在 `parse_sql` 捕获到多插入形态时抛出一个**可识别的业务异常**，
  让 `workflow.py` 把它记成「多插入语法暂不支持」而不是一条裸的 `ValueError`。

### P0-2 标量子查询被误判为常量（样本 04）—— 建议：先判子查询，再判常量

* **问题定位**：`lineage/parser.py::_collect_column_lineage` 里
  `if not sources and not self._own_columns(expr): sources = [(None, CONSTANT_MARKER, True)]`。
  `_own_columns` 会剪掉子查询，于是「含子查询的表达式」被误判成「纯常量表达式」。
* **建议做法**（约 6 行）：
  ```python
  has_subquery = bool(expr.find(exp.Subquery)) if isinstance(expr, exp.Expression) else False
  if not sources and not self._own_columns(expr):
      if has_subquery:
          sources = [(None, "(子查询)", False)]      # 未解析，绝不冒充常量
      else:
          sources = [(None, CONSTANT_MARKER, True)]
  ```
* **加分项**：同时把子查询 `FROM` 里的表并进 `input_tables`（见 P1-1），这样
  `brand_max_sale_qty` 虽然字段级未解析，但表级血缘不丢。
* **验收**：样本 04 的 `brand_max_sale_qty` 必须变成 `resolved=false` + `source_column='(子查询)'`；
  现有 `test_parser.py` 里断言「常量」的用例只有真正无列的表达式（`COUNT(1)`、`'自产'`）才应命中。

### P1-1 WHERE / 子查询里的表没进 `input_tables`（样本 04）—— 建议：补一轮「游离表」扫描

* **问题定位**：`_collect_physical_tables` 只走每个 `Scope` 的 `from_` 与 `joins`，
  不扫 `WHERE` 里的 `EXISTS` / `IN` / 标量子查询。
* **建议做法**：收集完作用域内的表后，对当前 `Select` 的 `where` / `having` 节点做一次
  `find_all(exp.Table)`，跳过已在 `Scope` 里出现过的（含 CTE 名），把剩下的物理表并入 `input_tables`
  与 `table_lineage`，并加一个 `via` 字段标明来源（`"where_subquery"`），方便人工判断。
* **验收**：样本 04 的 `input_tables` 应包含 `dim.dim_brand`（1 次）与 `ods.ods_卷烟销量流水`。

### P1-2 MERGE INTO 完全不出血缘（样本 19）—— 建议：给 `_classify` 加 Merge 分支

* **问题定位**：`_classify` 只认 `exp.Insert` / `exp.Create` / 查询，`exp.Merge` 落到 `OTHER`。
* **建议做法**：
  ```python
  if isinstance(statement, exp.Merge):
      target = statement.this                      # exp.Table -> 目标表
      ref = TableRef.from_ast(target) if isinstance(target, exp.Table) else None
      using = statement.args.get("using")          # exp.Subquery / exp.Table
      on = statement.args.get("on")
      query = self._select_from_merge(using, on)   # 用 using 的 SELECT，ON 里的表并入 input
      return TASK_MERGE, ref, query
  ```
  新增 `TASK_MERGE = "MERGE"` 常量；字段级血缘可让 `INSERT`/`UPDATE SET` 的列映射各自产出
  （`WHEN MATCHED THEN UPDATE SET t.x = s.x` → `t.x <- s.x`）。
* **验收**：样本 19 应产出 `ods.ods_卷烟产量流水 → cdw.dwd_产量明细` 1 对表级血缘 + 3 条字段级血缘。

### P1-3 `CREATE TABLE ... LIKE ...` 目标表丢失（样本 10）—— 建议：识别 `LikeProperty`

* **问题定位**：`_classify` 对 `CREATE` 只认 `body` 是查询的 CTAS。
* **建议做法**：在 `CREATE` 分支里先取 `ref = TableRef.from_ast(statement.this)`（目标表一定在 `this`），
  再判断：
  * `body` 是查询 → `TASK_CTAS`（现状不变）；
  * `properties` 里存在 `exp.LikeProperty` → 返回 `TASK_OTHER` 但**带上 `output_refs=[目标表]`**，
    并把 `LikeProperty` 里的表（`b`）作为输入表 → 血缘 `b -> a`；
  * 显式列定义建表（`CREATE TABLE a (x STRING)`）→ 输出 `output_refs=[a]`，输入为空。
* **验收**：样本 10 的 `output_tables` 应为 `[cdw.dwd_产量明细_归档, cdw.dim_班次]`，
  且 `cdw.dwd_卷烟产量明细 -> cdw.dwd_产量明细_归档` 出现在表级血缘里。

### P2-1 UDTF / LATERAL VIEW 输出列无法回溯（样本 06、17）—— 建议：给 UDTF 输出列建「人可读的未解析记录」

* **现状**：`tag.t` / `pos.p` / `tag.brand` 记 `resolved=false` + `source_table=别名`，
  已经比「瞎猜」好；但使用者不知道这个别名来自哪个 `explode(...)`。
* **建议做法**：解析 `LATERAL VIEW <fn>(...) <alias> AS c1, c2`，把
  `(别名, 列名) -> {udtf: "explode", 输入表达式: "split(o.tag_list, ',')", 底表: "ods.ods_卷烟产量流水"}` 记进
  `column_lineage[].unresolved_reason`，至少让人知道「这列是 UDTF 展开出来的，源列是 `o.tag_list`」。
  进一步（需要元数据）可以用 `explode(split(col, ','))` 的源列建立**列级血缘的弱关联**
  （`tag_name <- o.tag_list`，标 `confidence="weak"`）。

### P2-2 `SELECT *` 缺列（样本 12）—— 建议：用「建表语句 / 维表」补全（可选增强）

* **现状符合声明**（不报错、明确 unresolved），但字段级血缘仍然缺失。
* **建议做法**（按成本从低到高）：
  1. **脚本内建表补全**：`scan.py` 已经能看到同一目录下所有 SQL。收集
     `CREATE TABLE t (col ...)` 的列清单做一份内存「轻量表结构」，遇到 `SELECT *` 时按它展开，
     展开出来的列标 `resolved=true` + `inferred_from="create_table"`；
  2. **知识库补全**：`data/knowledge.db` 里已有字段术语表（`kb fields`），可作为第二数据源；
  3. **显式开关**：给 `analyze` 请求加 `expand_star=create_table|kb|off`（默认 `off`，保持现有行为不变），
     避免「补全错了」比「不补」更糟。

### P2-3 动态分区值未记录（样本 08）—— 建议：记录「动态分区键」

* **现状**：`INSERT ... PARTITION (dt) SELECT ..., o.work_date AS dt` 血缘正确，
  但 `partition_filters` 为空，不知道 `dt` 是动态分区、值来自 `SELECT` 最后一列。
* **建议做法**：在 `_collect_partition_filters` 里，对无边界的 `PARTITION (dt)` 记录
  `partition_filters={"dt": "(动态)"}`，并可对齐 `SELECT` 最后一列给出
  `dynamic_partition_source={"dt": "o.work_date"}`。这样分区语义在白屏/报告里能看出来。

### P2-4 sqlglot warning 噪音（样本 17）—— 建议：解析前捕获 warning

* `NAMED_STRUCT` 会往 stderr 打 `Hive does not support named structs.`。在 CLI / 服务端日志里是噪音。
* **建议做法**：在 `SqlLineageParser.parse_sql` 里用 `warnings.catch_warnings()` +
  `logging` 捕获 sqlglot 的 logger，把这类提示收进 `result["warnings"]`（或直接降噪），
  既不丢信息也不污染日志。

---

## 4. 已知边界之外仍然可靠的场景（回归基线）

以下形态实测**完全正确**，可作为回归保护的基线（对应 `tests/test_parser.py` 与本文档表格）：

* 多层 CTE / 派生表链式引用，血缘下推到真实物理表；
* 窗口函数（含 `ROWS BETWEEN`）、嵌套聚合、复杂表达式（CASE / COALESCE / CAST / NULLIF / 字符串函数）；
* 5 表 JOIN（LEFT / INNER / FULL OUTER 混用）、`LEFT SEMI JOIN` / `LEFT ANTI JOIN`；
* `UNION ALL` 三分支（按位置对齐目标列）；
* `GROUPING SETS` / `ROLLUP` / `CUBE`；
* CTAS（含 `COMMENT` / `STORED AS` / `TBLPROPERTIES`）；
* 静态分区（多分区键值正确落入 `partition_filters`）、`INSERT INTO TABLE`；
* 中文表名 / 中文字段名 / 全角括号别名（`AS 产量（箱）`）。

---

## 5. 证据文件

| 文件 | 内容 |
| --- | --- |
| `examples/complex_sql/*.sql` | 19 个固定回归样本（每个样本头部注释写明特性） |
| `demos/complex_sql_probe.py` | 实测探针（支持编号过滤、字段级明细、落 JSON） |
| `reports/complex_sql_probe.txt` | 全量实测终端输出（本文档引用的原始片段来源） |
| `reports/complex_sql_probe.json` | 结构化实测结果（每条语句的表级/字段级血缘、未解析明细、分区、JOIN） |

> 本矩阵是**当前实现（sqlglot 30.18.0）的实测快照**。改进落地后，重跑探针即可刷新；
> 建议把 P0/P1 验收点补进 `tests/`，避免回归。
