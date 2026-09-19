# 演示手册 · SQL 血缘解析与数据资产智能运营平台

> 用途：面试现场演示脚本（照此执行即可，约 8-10 分钟）
> 项目路径：`/root/projects/sql-lineage-mvp`
> 在线演示页：https://bumpc6k.github.io/sql-lineage-mvp/

---

## 0. 环境准备（面试前 5 分钟做）

```bash
# ① 启动 DolphinScheduler 演示环境（如未运行）
docker start ds-standalone || docker run -d --name ds-standalone -p 12345:12345 \
  apache/dolphinscheduler-standalone-server:3.2.2

# ② 进入项目
cd /root/projects/sql-lineage-mvp

# ③ 自检（应输出 205 passed）
.venv/bin/python -m pytest -q
```

---

## 1. 演示流程（7 步）

### Step 1｜开场定位（30 秒，不用跑命令）

**话术**：
> "我做了一个**数据血缘分析工具**，解决的问题是：数仓里上千个加工脚本，业务口径和依赖关系散落在 SQL 里，新人看不懂、改表不知道影响谁、数据治理无从下手。
>
> 它能做三件事：**① 从脚本自动解析血缘 ② 构建全局图谱做影响分析 ③ 对接 DolphinScheduler 打通'血缘 × 调度'**。"

---

### Step 2｜SQL 血缘解析（1 分钟）

```bash
.venv/bin/python -m lineage.cli examples/05_pipeline_multi_statement.sql
```

**预期输出**（真实）：
```
SQL 血缘解析报告  dialect=hive  文件数=1  语句数=3

[语句 1] task_type = CTAS
  输出表  : dwd.dwd_烟叶采购明细
  表级血缘: ods.ods_烟叶采购 --> dwd.dwd_烟叶采购明细
  字段级血缘 (7 条):
    dwd.dwd_烟叶采购明细.total_amt <- ods.ods_烟叶采购.leaf_weight  [c.leaf_weight * c.unit_price AS total_amt]
  分区过滤: dt=2026-01-01
```

**讲解点**：
- 支持 **CTAS / INSERT_SELECT / 多表 JOIN / 子查询 / CTE / UNION** 多种形态
- **字段级血缘**能追到计算表达式（`leaf_weight * unit_price`），这是后续"口径提炼"的基础
- 支持多方言（hive / spark / doris / postgres）

---

### Step 3｜目录扫描 → 全局血缘图（1.5 分钟）

```bash
.venv/bin/python -m lineage.cli scan examples/warehouse --graph-out warehouse_graph.json
```

**预期输出**（真实）：
```
SQL 文件数：21    语句数：24
表（节点）数：34    血缘边数：41    字段级映射：215 条
源表 10 张 / 叶子表 1 张 / 孤立表 0 张 / 最大血缘深度 7 层
分层分布：src=6  ods=6  dim=4  dwd=6  dws=6  ads=6
循环依赖：无 ✔
```

**讲解点**：
- 扫一整个数仓脚本目录，**跨文件血缘自动贯通**（ODS 文件里的产出表 = DWD 文件里的输入表）
- 自动统计分层分布、源表/叶子表、**最大血缘深度 7 层**
- 顺带做**循环依赖检测**（调度无法确定顺序的风险）

---

### Step 4｜影响分析 ⭐ 重点（1.5 分钟）

```bash
.venv/bin/python -m lineage.cli impact ods.ods_卷烟产量流水 --graph warehouse_graph.json
```

**预期输出**（真实）：
```
下游影响分析（改这张表会波及谁）
起点表：ods.ods_卷烟产量流水
  第 1 层（1 张）：cdw.dwd_卷烟产量明细
  第 2 层（1 张）：cdw.dws_产量汇总
  第 3 层（2 张）：cdw.dws_产销存汇总, cdw.dws_税利汇总
  第 4 层（2 张）：ads.ads_产销存月报, ads.ads_税利分析
  第 5 层（1 张）：ads.ads_经营指标明细
  第 6 层（1 张）：ads.ads_经营指标驾驶舱
合计下游表数：8 张    涉及血缘边：10 条
```

**讲解点**（这是数据治理最刚需的能力）：
- "改一个 ODS 字段，**6 层链路、8 张下游表**一目了然，直到经营驾驶舱"
- 这就是变更评估、故障定位、废弃表清理的基础能力

---

### Step 5｜上游溯源（1 分钟）

```bash
.venv/bin/python -m lineage.cli upstream ads.ads_经营指标驾驶舱 --graph warehouse_graph.json
```

**预期输出**（真实）：
```
第 1 层（3 张）→ 第 2 层（4 张）→ 第 3 层（6 张）→ 第 4 层（8 张）
→ 第 5 层（6 张）→ 第 6 层（4 张）→ 第 7 层（2 张）
合计上游表数：33 张    涉及血缘边：41 条
最上游源表（10 张）：src.erp_生产工单明细、src.mes_销售出库明细、src.iot_设备工况采集...
```

**讲解点**：
- "看板上的一个数字，能追溯到**33 张上游表、10 个源系统**"
- 数据质量问题的根因定位、口径排查全靠它

---

### Step 6｜对接 DolphinScheduler ⭐ 亮点（2 分钟）

```bash
# 从真实海豚实例拉取（旁路集成，不改海豚一行源码）
.venv/bin/python -m lineage.cli ds sync

# 查某张表被哪个工作流、哪个任务加工
.venv/bin/python -m lineage.cli ds tables ods.ods_卷烟产量流水 --graph ds_lineage.json

# 查某张表的上游 + 每张上游表的调度产出方
.venv/bin/python -m lineage.cli ds upstream ads.ads_经营指标驾驶舱 --depth 2 --graph ds_lineage.json
```

**预期输出**（真实）：
```
【工作流依赖拓扑】
  第 1 层：wf_ods_采集
  第 2 层：wf_dwd_清洗
  第 3 层：wf_dws_汇总
  第 4 层：wf_ads_报表
  依赖明细：
      wf_ods_采集 -> wf_dwd_清洗（表血缘，经表 ods.ods_卷烟产量流水...）
      wf_dws_汇总 -> wf_ads_报表（海豚原生，由任务 t_check_上游就绪）

【各上游表的调度产出方（DolphinScheduler 侧）】
  ads.ads_产销存月报（ads）<- wf_ads_报表/t_ads_产销存月报
  ads.ads_税利分析（ads）<- wf_ads_报表/t_ads_税利分析
```

**讲解点**（最有杀伤力的部分）：
- **"血缘 × 调度"打通**：不只知道表依赖，还知道**是哪个工作流的哪个任务在产出它**
- **旁路集成**：只调 OpenAPI 只读，**不改海豚一行源码** → 海豚升级无痛，同样思路可移植到 DataWorks / DataArts
- 依赖推导用了**两条线索**：表血缘（我算的）+ 海豚原生 DEPENDENT 依赖（平台声明的）

---

### Step 7｜可视化展示（30 秒）

```bash
# 交互式 HTML 已生成，浏览器直接打开
# docs/lineage.html（数仓血缘图谱）
# docs/ds_lineage.html（调度血缘）
```

**或直接打开在线演示页**：https://bumpc6k.github.io/sql-lineage-mvp/

**讲解点**：
- "这是**自包含 HTML**（零外链），离线也能打开；点击节点能高亮上下游、搜索表名、分层筛选"
- 图谱渲染是**手写的 canvas 力导向引擎**（没用第三方库，避免依赖）

---

## 2. 关键数字速查（被问就报）

| 指标 | 数值 |
|------|------|
| 单元测试 | **205 passed**（P1 26 / graph 45 / scan 23 / viz 18 / ds_client 48 / ds_lineage 45）|
| 数仓示例 | 21 个 SQL 脚本 → 34 表 / 41 边 / 215 字段映射 / 7 层深度 |
| 影响分析 | 改 1 张 ODS 表 → 8 张下游表 / 6 层链路 |
| 上游溯源 | 驾驶舱 → 33 张上游表 / 10 张源表 / 26 条链路 |
| 海豚集成 | 1 项目 / 4 工作流 / 16 任务 / 23 表 / 15 份 SQL |
| 代码量 | parser 922 + graph 1083 + scan 320 + viz 782 + ds_client 658 + ds_lineage 1254 行 |

---

## 3. 追问预演（高频 10 问）

| 追问 | 答题要点 |
|------|---------|
| **和 DataHub / Atlas 什么区别？** | 它们做**技术血缘**（表/字段依赖），但业务口径（如"产量=打码量+跳码量−重码量"）仍靠人工维护术语表；我下一步做**从脚本自动提炼口径**，这是空白点 |
| **为什么不用现成的血缘工具？** | ① 部署重、改造成本高 ② 我们要的是"口径知识"不是再造元数据平台 ③ 自研可控，且能同时对接多个调度平台 |
| **为什么改海豚源码 / 不写插件？** | 海豚迭代快（3.4 已发布），改源码=升级地狱；SPI 插件面向"任务执行/数据源"扩展，血缘是**旁路分析服务**，不适合塞进插件。所以走 OpenAPI 只读旁路集成 |
| **`SELECT *` 怎么处理？** | 当前标 `resolved=false`（无元数据无法展开）→ P4 接 Hive Metastore / DDL 解析解决（**知道边界比假装能做更重要**）|
| **血缘准不准？** | 静态解析的边界：动态 SQL（变量拼接）需先渲染；非 SQL 脚本（Python 拼 SQL）用正则+LLM 兜底并标注置信度 |
| **几十张表能跑，上千张呢？** | 当前内存图 + JSON 落盘，实测 34 节点毫秒级；上千节点应上**图数据库（Neo4j/Nebula）**，架构里已留好抽象层（`to_dict/from_dict` 可换后端）|
| **怎么保证性能？** | 解析是纯 AST 遍历（21 文件 0.042 秒）；扫描支持忽略目录；后续加**增量扫描（按 mtime 差分）** |
| **口径提炼准确率怎么保证？** | LLM 生成候选 + **证据链（来源脚本/责任人）** + **人工审核强制环节** + 版本管理可回滚 —— 口径错会污染全公司指标，不能全自动 |
| **这跟智能问数什么关系？** | 它是问数的**知识供给层**：把口径知识注入 Text-to-SQL，让问数从"语义猜"变成"有据可依"，准确率才上得去 |
| **后续规划？** | P4 口径知识库 + 智能问数 → P5 智能运维（SQL 反模式检测、异常预警）→ P6 自动生成 SQL/调度链路（人工确认后落地）|

---

## 4. 演示注意事项

- ✅ **提前跑一遍**：确保海豚容器在跑、`.venv` 依赖完好、`warehouse_graph.json` 与 `ds_lineage.json` 存在
- ✅ **命令用 `--graph xxx.json` 指定数据**：避免连不上环境时卡住（大部分查询命令只读本地 JSON，不需要海豚在线）
- ⚠️ 只有 `ds sync` 需要海豚在线（其他 `ds xxx` 子命令都读本地 JSON）
- 💡 演示时先给结论再给命令（"我能查下游影响——你看这条命令…"），比先跑命令再解释更抓人
