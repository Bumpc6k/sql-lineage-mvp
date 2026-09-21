# P5「血缘 × 业务口径一体化」证据

本目录是 P5 的**原始证据留档**（命令 + 真实输出），一键重跑：

```bash
bash evidence/p5_verify_all.sh
```

| 文件 | 是什么 | 复现命令 |
| --- | --- | --- |
| `01_ds_task_log_sql_mode.txt` | **核心证据**：海豚任务实例日志原文（①~⑤ 五段报告 + varPool），SQL 用 `examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql` | `bash /usr/local/bin/prep-ds-demo.sh` → `.venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py` |
| `02_ds_task_log_impact_mode.txt` | `mode=impact` 回归：①②③ 段 + 下游影响段仍在，第 ⑤ 段不出现 | `.venv/bin/python apps/apps/ds-plugin/verify/verify_impact_mode.py` |
| `03_ds_task_log_degraded.txt` | 降级：服务端为「旧版」（`/analyze` 404）时，插件回退 `/parse`，任务仍 SUCCESS，⑤ 段退化为一行提示 | `bash evidence/p5_collect_degraded_evidence.sh` |
| `04_http_endpoints.txt` | 全部 HTTP 端点真实请求：`/health`、`/parse`、`/analyze`、`/impact`、`/upstream`、`/kb/*`，以及 `/analyze` 的四种边界 | `bash evidence/p5_regression.sh` |
| `05_pytest.txt` | 单元测试全量结果（269 passed） | `env -u http_proxy -u https_proxy -u all_proxy .venv/bin/python -m pytest -o addopts="" -q` |
| `06_plugin_build_deploy.txt` | 插件编译 + `docker cp` 四个 libs + 重启海豚的尾部输出 | `bash apps/apps/ds-plugin/java/build.sh` |
| `07_prep_ds_demo.txt` | 演示环境恢复（插件加载 + 演示数据 + 端到端验证） | `bash /usr/local/bin/prep-ds-demo.sh` |
| `08_analyze_endpoint.txt` | `POST /analyze` 的完整响应解读（含 `knowledge.metrics[chanliang_qty]` 原文 JSON） | `.venv/bin/python evidence/p5_analyze_evidence.py` |

关键结论（三句话）：

1. `/analyze` 是 `/parse` 的严格超集：血缘字段一字不变，只多 `knowledge` 段（口径 / 术语 / 规则 / 链路）。
2. 海豚 LINEAGE 插件任务日志新增第 ⑤ 段：`产量（chanliang_qty） = 打码量 + 跳码量 - 重码量`，
   并给出类型、置信度、来源脚本、依赖字段与「cdw → ods → src」上游链路；出参新增
   `lineage_metric_count` / `lineage_metric_names`。
3. 旧端点与 `impact`/`upstream` 模式零回归；知识库缺失 / 空库 / 旧版服务 / 匹配不到，全部优雅降级。
