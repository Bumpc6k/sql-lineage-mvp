# 模拟数仓脚本目录（烟草行业场景）

这是 `lineage.cli scan` 的示例输入：一个模拟真实数仓分层的 SQL 脚本目录。

| 目录 | 分层 | 内容 |
| --- | --- | --- |
| `ods/` | ODS 贴源层 | 6 张贴源表，从 `src.*` 源系统接口表抽取（产量 / 销量 / 库存 / 税利 / 烟叶采购 / 设备工况） |
| `cdw/` | DWD + DWS | 6 张明细事实表（`dwd_*`）+ 6 张汇总表（`dws_*`，其中 `dws_产销存汇总.sql` 是 3 条语句的多语句文件） |
| `ads/` | ADS 应用层 | 6 张应用报表，其中 `ads_经营指标驾驶舱.sql` 是 2 条语句的多语句文件，位于全图最末端 |

血缘链路示例（跨文件贯通）：

```text
src.erp_生产工单明细
  -> ods.ods_卷烟产量流水
  -> cdw.dwd_卷烟产量明细
  -> cdw.dws_产量汇总
  -> cdw.dws_产销存汇总
  -> ads.ads_产销存月报
  -> ads.ads_经营指标明细
  -> ads.ads_经营指标驾驶舱
```

`examples/warehouse_issues/` 是**故意做坏**的样例（循环依赖 + 语法错误），
用来演示 `cycle` 子命令与扫描报告的「解析失败清单」。

```bash
.venv/bin/python -m lineage.cli scan examples/warehouse --graph-out /tmp/warehouse_graph.json
```
