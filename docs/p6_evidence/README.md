# P6 证据留档：工作流级血缘（`LINEAGE_DAG` + `POST /analyze-workflow`）

本目录是**真实跑出来的原始输出**，不是手写示例。复现命令见每节末尾。

| 文件 | 内容 | 复现命令 |
| --- | --- | --- |
| `01_analyze_workflow_and_frontend.txt` | `POST /analyze-workflow` 的 curl 证据（HTTP 200 + `wf_dwd_清洗` 的任务清单 / 合并表级血缘 / 链路质量体检 / 报告 URL）、报告页 `GET /report/<id>` 的 HTTP 状态与 HTML 前 15 行、4 个前端 bundle 的 200 + 补丁串检查、容器内动态任务类型配置 | `bash evidence/collect_dag_evidence.sh` |
| `02_ds_task_log_lineage_dag.txt` | `LINEAGE_DAG` 任务实例日志**全文**（两次运行：3 个 SQL 任务 + 同层 DAG 节点；给历史工作流 `wf_dws_汇总` 追加的尾节点），含五段式报告与 varPool 出参 | `bash apps/apps/ds-plugin/java/build.sh && bash ops/prep_ds_demo.sh && .venv/bin/python apps/apps/ds-plugin/verify/verify_dag.py` |
| `03_analyze_workflow_response.json` | `/analyze-workflow` 的完整 JSON 响应（32 KB，未删减） | 同上（curl 那一步） |
| `04_workflow_lineage_report.html` | 工作流级 HTML 报告（单文件、零外部依赖；顶部「工作流概览」+ 任务清单 + 链路质量体检 + 跨任务字段血缘 + 全链路 DAG SVG） | 浏览器打开，或 `curl http://localhost:18080/report/<report_id>` |
| `05_lineage_regression.txt` | **回归**：现有 `LINEAGE`（单脚本）任务跑一次，①~⑤ 段 + 表格折叠 + 报告 URL 全部命中断言 | `.venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py` |
| `06_pytest.txt` | 全量单测（**312 passed**，含新增 20 个工作流级用例） | `env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY .venv/bin/python -m pytest -o addopts="" -q` |

## 一句话结论

* 服务端 `POST /analyze-workflow`：一次调用拉整个工作流的脚本批量解析，返回
  任务清单 / 合并表级血缘（跨任务）/ 跨任务字段血缘 / 全链路 `chain`（含全局血缘拼接）/
  口径汇总 / 链路质量体检（断链·孤岛·环路·未登记口径），并落一份工作流级 HTML 报告；
* 插件 `LINEAGE_DAG`：编译部署成功、海豚启动日志里 `Registered task plugin: LINEAGE_DAG`，
  在含 3 个 SQL 任务的工作流里**真跑成功**并打印五段式中文报告 + 可点击报告 URL；
* 历史工作流零改造：给 `wf_dws_汇总` 追加一个尾节点，原有 4 个 SQL 任务一行未改，
  报告里就是那 4 条真实 SQL 的全链路；
* 回归：现有 `LINEAGE` 单脚本任务与 312 个单测全绿。

## 遗留（如实说明）

* 前端侧没有真浏览器验证（本环境浏览器工具调用超时）：给的是 HTTP 200 + 补丁串 +
  与镜像原版 1 行 diff + QuickJS 真跑补丁片段的结构性证据，**需要人工 Ctrl+Shift+R 后目视确认一次**；
* 演示环境无真实 HIVE 数据源 ⇒ SQL 任务 FAILURE ⇒ 海豚不提交失败上游的下游任务，
  所以「尾部串联」的节点在本环境会被跳过（同层挂载能真跑，两者报告一致）。
