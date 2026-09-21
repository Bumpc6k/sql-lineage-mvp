# DolphinScheduler 自定义任务类型插件：LINEAGE / LINEAGE_DAG（血缘分析）

给 Apache DolphinScheduler **3.2.2**（standalone 单体容器 `ds-standalone`）加**两个**自定义任务类型：

| 任务类型 | 粒度 | 用法 | 章节 |
|---|---|---|---|
| `LINEAGE` | 单条 SQL / 单张表 | 参数里填 SQL 或表名，解析这一条 | 第 1~6 章 |
| `LINEAGE_DAG` | **一整个工作流** | 挂在工作流尾部（或任意位置），**不用填任何 SQL**：运行时自动拉取本工作流全部任务脚本批量解析，输出全链路图谱 + 跨任务字段血缘 + 口径汇总 + 链路质量体检 | **第 7 章** |

两者都通过 HTTP 调用宿主上的血缘服务（`http://172.17.0.1:18080`），把报告写进**任务实例日志**，
并把关键结果写成**出参**（`varPool`）供下游任务引用。

- 插件类型名：`LINEAGE` / `LINEAGE_DAG`
- 插件实现：`org.apache.dolphinscheduler.plugin.task.lineage.*`
- 依赖：只依赖海豚自身的 `dolphinscheduler-task-api` / `dolphinscheduler-spi` /
  `dolphinscheduler-common`（`JSONUtils`）+ `jackson` + `slf4j`，HTTP 用 JDK 自带
  `HttpURLConnection`，**不需要额外第三方库**
- 注册方式：手写 `META-INF/services/org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory`
  （不用 `@AutoService`，避免引入 auto-service 依赖）

---

## 1. 目录结构

```
sql-lineage-mvp/apps/ds-plugin/          ← 【独立部署单元③】海豚插件（可整体替换，不依赖后端代码）
├── java/                                # ── 编译层（Java 侧唯一真相）
│   ├── build.sh                         # 编译 + 打包 + 拷进容器 + 重启海豚（一键）
│   ├── src/main/java/org/apache/dolphinscheduler/plugin/task/lineage/
│   │   ├── LineageTaskChannelFactory.java   # SPI 入口：getName()="LINEAGE"，getParams() 定义 UI 表单
│   │   ├── LineageTaskChannel.java          # TaskChannel：createTask / parseParameters
│   │   ├── LineageParameters.java           # 任务参数（继承 AbstractParameters，带 checkParameters）
│   │   ├── LineageTask.java                 # 核心：拼 JSON -> HTTP POST -> 解析 -> 打日志 -> 写出参
│   │   ├── LineageServiceClient.java        # JDK HttpURLConnection 封装的极小 HTTP 客户端
│   │   ├── LineageDagTaskChannelFactory.java# 【第二个任务类型】SPI 入口：getName()="LINEAGE_DAG"
│   │   ├── LineageDagTaskChannel.java       # TaskChannel（工作流级）
│   │   ├── LineageDagParameters.java        # 任务参数：scope/taskTypes/includeSubProcess/…（无 SQL 字段）
│   │   └── LineageDagTask.java              # 工作流级核心：拉整个工作流 → 批量解析 → 五段式日志
│   ├── src/main/resources/META-INF/services/
│   │   └── org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory
│   ├── dolphinscheduler-task-lineage-3.2.2.jar   # 构建产物（唯一应部署的 jar）
│   ├── libs/                            # 从容器导出的编译依赖 jar（build.sh 自动导出）
│   └── build/                           # 编译中间产物 + verify_output.txt（验证原始输出）
├── frontend/                            # ── 前端补丁层（改的是海豚自带 UI bundle）
│   ├── conf/dynamic-task-type-config.yaml    # 改过的海豚配置（源文件，容器内是副本）
│   └── ui-static/
│       ├── lineage/{lineage.json, lineage-icon.svg, lineage-hover.svg}
│       ├── lineage/{lineage-dag.json, lineage-dag-icon.svg, lineage-dag-hover.svg}
│       └── assets/                      # 打过补丁的前端 bundle（侧边栏 / 类型表 / 节点设置弹窗）
├── deploy/deploy_ui.sh                  # 把动态任务类型配置/表单 JSON/图标拷进容器并重启（UI 可见用）
├── verify/                              # ── 验证层（可独立重跑，输出即证据）
│   ├── verify.sh                        # 部署与功能验证（真实请求，输出即证据）
│   ├── verify_api.py                    # verify.sh 第 6 步调用的端到端脚本（被 verify.sh 调用）
│   ├── verify_knowledge.py              # 「产量口径」SQL 端到端：断言 ①~⑤ + 表格/折叠/📊 报告 URL，并回读报告
│   ├── verify_impact_mode.py            # mode=impact 回归（第 ⑤ 段与报告行都不出现）
│   ├── verify_dag.py                    # LINEAGE_DAG 端到端：3 个 SQL + 尾节点 → 运行 → 拉日志全文
│   ├── verify_ldf_form.py               # 【前端表单】把 bundle 里的 LINEAGE_DAG 表单函数 LDF 原样抽出来真跑
│   │                                    #   + 把 json 喂给真实渲染管线 We()；--control 用 SQL 表单 Rr 做对照
│   ├── verify_save_params.py            # 【前端表单】把 LDF 的 model 喂给真实保存函数 Ne()，断言 taskParams 带全 4 字段
│   ├── patch_detail_ldf.py              # 【前端表单】插入专属表单函数 LDF 并改 oa 映射（幂等）
│   ├── patch_modal_taskparams.py        # 【前端表单】给 detail-modal 的保存白名单补 LINEAGE_DAG 一行（幂等）
│   └── verify_ui_deploy.py              # 【前端表单】服务端 4 个 bundle 200 / .gz 同步 / 内容里确实没有 SQL 字段
└── README.md                            # 本文件

```

## 2. 任务参数（taskParams）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `mode` | string | `sql` | `sql`=SQL 血缘解析；`impact`（兼容别名 `table`）=下游影响；`upstream`=上游溯源 |
| `sql` | string | - | `mode=sql` 时必填，多行 SQL |
| `dialect` | string | `hive` | 传给血缘服务的 SQL 方言 |
| `table` | string | - | `impact`/`upstream` 模式的起始表名（如 `dim.dim_plant`） |
| `depth` | int | `3` | 影响/溯源深度 |
| `graph` | string | `warehouse_graph.json` | 血缘图文件（服务端相对其项目根解析） |
| `serviceUrl` | string | `http://172.17.0.1:18080` | 血缘服务地址（容器内访问宿主的 docker 网关地址） |
| `timeout` | int | `30000` | HTTP 超时（毫秒） |

被调用的血缘服务端点：`POST /analyze`（`mode=sql`，血缘 + 业务口径一体化，**默认顺带生成 HTML 报告**）、
`POST /impact`、`POST /upstream`、`GET /health`。
`mode=sql` 时若 `/analyze` 不可用（服务端还是旧版本），会自动**回退**到 `POST /parse`，
并把回退原因写进任务日志，血缘报告照常输出（此时拿不到报告地址，日志里**安静略过**那一行）。

任务执行成功的判定：HTTP 2xx 且返回体不是 `{"code": 非0}` 业务错误。

输出参数已写入 varPool（下游任务可用 `${lineage_output_tables}` 之类引用，也能在
「工作流实例 → 查看变量」里看到）：

```
lineage_mode, lineage_service_url,
lineage_input_tables, lineage_output_tables,
lineage_input_table_count, lineage_output_table_count,
lineage_kb_available, lineage_metric_count, lineage_metric_names,
lineage_cost_ms, lineage_report_id, lineage_report_url, lineage_report_raw
```

`lineage_metric_count` = 本任务产出字段命中了几条知识库口径；
`lineage_metric_names` = 命中的口径中文名，逗号分隔（如 `产量,打码量（条）`）；
`lineage_report_id` / `lineage_report_url` = 本次生成的 HTML 报告 ID 与地址（旧版服务为空串）。

### 2.1 任务日志格式：五段式中文报告（精简 + 表格化）

**设计原则**：日志只放「人一眼能读完」的部分，读不完的都折叠成一行指向完整 HTML 报告。

| 段 | 内容 | 数据来源 |
|---|---|---|
| ① 表级血缘 | 数据流向（源表 ──► 目标表）+ 源表/目标表清单 | `/analyze` 或 `/parse` |
| ② 字段级血缘 | **紧凑表格**：`目标字段 │ 来源字段 │ 加工表达式`（列间固定分隔符对齐、CJK 按 2 列算宽度）；**最多 15 行**，超出打印「… 其余 N 行见完整报告」 | 同上 |
| ③ 加工条件 | 一条语句一行：`过滤条件 \| 分区条件` | 同上 |
| ④ 加工 SQL 原文 | 解析后的 SQL（`mode=impact/upstream` 时是分析目标表）；按显示宽度**按词折行**（100 列） | 同上 |
| ⑤ 业务口径 | **只展开最关键的 3 条**，每条 3 行（口径公式 / 类型·置信度·匹配方式·目标表 / 依赖+链路摘要）；其余口径、字段中文名、业务规则各折叠一行 | `/analyze` 的 `knowledge` 段（知识库 SQLite） |
| 📊 完整报告 | 日志末尾一行可点击 URL：`📊 完整报告（浏览器打开）: http://localhost:18080/report/<id>`（+ 容器内访问用 `172.17.0.1` 的地址） | `/analyze` 的 `report` 段 |

「最关键」的判据（与 HTML 报告同一套，不依赖知识库返回顺序）：
**类型（聚合 > 比率 > 算术 > 条件 > 函数 > 窗口）→ 置信度降序 → 依赖字段数降序**。

降级：知识库不存在 / 没命中 / 服务端是旧版本（回退 `/parse`）时，第 ⑤ 段退化成一行提示，
📊 报告行**整行不打印**（安静略过，不报错），①②③④ 与汇总行完全不受影响。

### 2.2 完整 HTML 报告（日志里那个可点击 URL）

日志装不下的东西都在报告里：**字段映射真表格（带关键字过滤）+ 口径卡片 + 上游链路 SVG**。
报告由血缘服务渲染成**单文件 HTML**（内联 CSS/JS/SVG，零外部依赖，断网/内网可开），
插件只是把服务端返回的地址打进日志。

```text
  📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_20260920_210602_1342968a
     （容器内访问用: http://172.17.0.1:18080/report/rpt_20260920_210602_1342968a）
```

报告内容（5 区块 + 页脚）：标题栏（任务/时间/耗时/源表→目标表/字段映射数/口径命中数/报告 ID）、
① 表级流向卡片、② 字段级血缘真表格（zebra + 一行内联 JS 过滤）、③ 业务口径卡片（公式 / 类型 /
置信度 / 来源脚本 / 依赖字段 / 上游链路）、④ 内联 SVG 上游链路图（左=最上游、右=本任务目标表）、
⑤ 加工条件 + SQL 原文、页脚「由 sql-lineage-mvp 生成」。

服务端侧（`lineage/report.py` + `lineage/api_server.py`）：

```bash
# 在宿主机上手动生成/回看一份报告（body 与 /analyze 一致）
curl -s -X POST http://127.0.0.1:18080/report -H 'Content-Type: application/json' \
     --data-binary @/tmp/lineage_report_body.json          # 见 evidence/report_evidence.sh
curl -s -D- -o /tmp/report.html http://127.0.0.1:18080/report/rpt_20260920_210602_1342968a
curl -s http://127.0.0.1:18080/reports | head -c 400      # 最近报告清单（JSON）
```

* `POST /analyze` 走 HTTP 时**默认也生成报告**（HTTP 层补齐 `with_report=true`），
  所以插件一次调用就能同时拿到分析结果与报告地址；body 里传 `with_report=false` 可关掉；
* 报告落盘 `reports/rpt_<时间戳>_<SQL 短 hash>.html`，只保留最近 200 份（旧报告被清理后
  `GET /report/<id>` 返回 404 HTML 页，可 `/reports` 看清单或重新生成）；
* URL 两个 host 的用途：`url`（`localhost:18080`）给 Windows/宿主浏览器点，
  `internal_url`（`172.17.0.1:18080`）给容器内任务访问；可用环境变量
  `LINEAGE_PUBLIC_BASE` / `LINEAGE_INTERNAL_BASE` / `LINEAGE_REPORTS_DIR` 覆盖；
* 报告是**附加产物**：生成失败只写服务端 stderr 并在响应里带 `report_error`，血缘接口照常返回。

报告长什么样（真实产物 30 357 字节 / 149 行）：

* 深色主题、`max-width:1200px` 单列布局、系统字体栈（`Microsoft YaHei` / `PingFang SC` / `Noto Sans CJK SC`），
  中文正常显示；全部 CSS/JS/SVG 内联，**没有一行外链**（`report_evidence.sh` 会断言这点）；
* ② 字段级血缘是**真表格**：表头吸顶（`position:sticky`）、奇偶行不同底色（zebra）、
  顶部一个输入框（一行内联 JS）按 `data-key` 实时过滤 17 行映射；
* ③ 业务口径是**卡片流**（`grid` 自适应，每张卡 430px 起）：公式代码块 + 类型/置信度/匹配方式徽标 +
  来源脚本 + 依赖字段 chips + 上游链路面包屑；
* ④ 上游链路图是**手写内联 SVG**：节点圆角方框 + `<marker>` 箭头，左=最上游、右=本任务目标表（绿框）。

> **关于截图**：本环境的浏览器工具调用会超时，所以**没有做像素级截图验证**。
> 结构/样式以上面的片段与 `bash evidence/report_evidence.sh` 的自检清单为准（10 项全 ✅）。
> 人工验收：在 Windows 浏览器打开日志里那行 `http://localhost:18080/report/<id>` 即可。

## 3. 编译与部署

```bash
cd /root/projects/ds-lineage-plugin
bash build.sh                 # 导出依赖 -> javac --release 8 -> 打包 -> docker cp -> 重启海豚
bash build.sh --no-restart    # 只编译部署不重启
```

`build.sh` 做的事：

1. 从 `ds-standalone` 容器导出编译依赖 jar 到 `libs/`（task-api / spi / common / jackson /
   slf4j / commons-* ）
2. `javac --release 8 -encoding UTF-8` 编译 `src/main/java` 下 5 个类
   （**必须 `--release 8`**：海豚运行时是 Java 8，本机 javac 是 17）
3. 打 `dolphinscheduler-task-lineage-3.2.2.jar`，并把 `META-INF/services/...` 注册文件一起塞进去
4. `docker cp` 到容器内 4 个 libs 目录（standalone 把 api/master/worker 合到一个进程，
   启动命令的 classpath 是 `/opt/dolphinscheduler/conf:libs/{alert,api,master,standalone,worker}-server/*`，
   放哪个目录都能被扫到，四个都放最稳）：
   `/opt/dolphinscheduler/libs/{worker-server,master-server,api-server,standalone-server}/`
5. `docker restart ds-standalone`，轮询 `http://localhost:12345/dolphinscheduler/ui` 直到 200

插件加载机制：`TaskPluginManager` 用 `PrioritySPIFactory` → `ServiceLoader.load(TaskChannelFactory.class)`
扫描 classpath 上的 `META-INF/services`，所以只要 jar 在 classpath 里就会被注册。

### 让 UI 也能选到「血缘分析」

海豚 3.2.2 前端把标准任务类型（SHELL/SQL/…）的别名映射**写死在前端 bundle**
（`ui/assets/task-type.*.js` 里的常量表），后端新加的 SPI 插件在前端没有对应 Vue 组件，
所以**只放 jar 的话 API 能用、UI 下拉里看不到**。海豚为此留了「动态任务类型」的口子：

- 后端读 `conf/dynamic-task-type-config.yaml`（`@PropertySource("classpath:...")`，
  `conf/` 在 classpath 上），通过 `/dolphinscheduler/dynamic/{category}/taskTypes` 暴露；
- UI 的 DAG 侧边栏正是调用这个接口渲染可拖拽的任务项，并按配置里的 `json` 地址拉取表单定义。

```bash
bash deploy_ui.sh    # 备份原配置 -> 拷贝 conf + ui-static -> 重启
```

改完后接口返回（真实响应，见 `build/verify_output.txt`）：

```json
GET /dolphinscheduler/dynamic/Universal/taskTypes
[{"name":"SHELL","hover":"/static/shell/shell-hover.png","icon":"/static/shell/shell-icon.png","json":"/static/shell/shell.json"},
 {"name":"LINEAGE","hover":"/ui/static/lineage/lineage-hover.svg","icon":"/ui/static/lineage/lineage-icon.svg","json":"/ui/static/lineage/lineage.json"}]
```

表单 JSON 由 `ui-static/lineage/lineage.json` 提供（HTTP 200 application/json 已验证），
结构与后端 `UiChannelFactory.getParams()` 的 `PluginParams` 一致。

## 4. 怎么在海豚里用

### 4.1 API

```bash
# 1) 登录（返回 sessionId，同时下发 sessionId cookie）
curl -s -X POST http://localhost:12345/dolphinscheduler/login \
     -d 'userName=admin&userPassword=dolphinscheduler123'

# 2) 建工作流 + 一个 LINEAGE 任务（form body；taskParams 可以是 JSON 对象也可以是其字符串）
curl -s -X POST "http://localhost:12345/dolphinscheduler/projects/<projectCode>/process-definition" \
  -b "sessionId=<sid>" \
  --data-urlencode 'name=wf_lineage_血缘分析演示' \
  --data-urlencode 'executionType=PARALLEL' \
  --data-urlencode 'globalParams=[]' \
  --data-urlencode 'locations=[{"taskCode":<taskCode>,"x":200,"y":200}]' \
  --data-urlencode 'taskRelationJson=[{"name":"","preTaskCode":0,"preTaskVersion":0,"postTaskCode":<taskCode>,"postTaskVersion":1,"conditionType":"NONE","conditionParams":{}}]' \
  --data-urlencode 'taskDefinitionJson=[{"code":<taskCode>,"name":"t_lineage_订单血缘","version":1,"description":"","delayTime":0,"taskType":"LINEAGE","taskParams":{"mode":"sql","sql":"INSERT INTO dwd.t_order SELECT id, amt FROM ods.t_order_src","dialect":"hive","serviceUrl":"http://172.17.0.1:18080","timeout":30000,"localParams":[],"resourceList":[],"varPool":[]},"flag":"YES","isCache":"NO","taskPriority":"MEDIUM","workerGroup":"default","environmentCode":-1,"failRetryTimes":0,"failRetryInterval":1,"timeoutFlag":"CLOSE","timeoutNotifyStrategy":"","timeout":0,"taskExecuteType":"BATCH"}]'

# 3) 上线 + 运行
curl -s -X POST "http://localhost:12345/dolphinscheduler/projects/<pc>/process-definition/<wfCode>/release?releaseState=ONLINE" -b "sessionId=<sid>"
curl -s -X POST "http://localhost:12345/dolphinscheduler/projects/<pc>/executors/start-process-instance" -b "sessionId=<sid>" \
  -d 'processDefinitionCode=<wfCode>&scheduleTime=2026-01-01%2000:00:00&failureStrategy=CONTINUE&execType=START_PROCESS&warningType=NONE&runMode=RUN_MODE_SERIAL&processInstancePriority=MEDIUM&workerGroup=default&tenantCode=default&environmentCode=-1'

# 4) 看任务日志
curl -s "http://localhost:12345/dolphinscheduler/log/detail?taskInstanceId=<id>&skipLineNum=0&limit=2000" -b "sessionId=<sid>"
```

> 【坑】`releaseState` 用的是枚举名 `ONLINE` / `OFFLINE`，传 `1`/`0` 会返回 10108
> “release process definition error”。`taskRelationJson` 用
> `conditionType`/`conditionParams`，用 `conditionResult`/`switchResult` 会在后端解析时
> 抛 “Check task relation list error, meet an unknown exception”。
> `taskCode` 用 `GET /projects/{pc}/task-definition/gen-task-codes?genNum=N` 取。

### 4.2 UI

工作流定义 → DAG 画布左侧任务列表里会出现「LINEAGE」（来自动态任务类型），拖到画布上即可；
表单字段由 `ui-static/lineage/lineage.json` 驱动（解析模式 / SQL / 方言 / 表名 / 深度 / 图文件 /
服务地址 / 超时）。保存并运行后，任务实例日志里能看到血缘报告。

## 5. 验证

```bash
cd /root/projects/sql-lineage-mvp/ds-plugin
bash verify.sh                            # 全量：部署检查 + 端到端跑一次（t_order 示例 SQL）
bash verify.sh --skip-run                 # 只做 1~5（不跑工作流）
../.venv/bin/python verify_knowledge.py   # 用「产量口径」SQL 跑一次，断言 ①~⑤ 全部出现
../.venv/bin/python verify_ldf_form.py    # LINEAGE_DAG 节点设置表单：抽 LDF 原样真跑 + 真跑渲染管线
../.venv/bin/python verify_ldf_form.py --control   # 对照：同一套断言跑 SQL 表单 Rr（必须 FAIL 17 项）
../.venv/bin/python verify_save_params.py # 表单→保存：LDF 的 model 喂给真实 Ne()，断言 taskParams 带全 4 字段
../.venv/bin/python verify_ui_deploy.py   # 服务端 4 个 bundle 200 / .gz 同步 / 内容不含 SQL 字段
```

`verify.sh` 的输出（摘录，完整见 `build/verify_output.txt`）：

```
==> 2/6 插件 jar 在容器内（worker/master/api/standalone 四个 libs 目录）
-rw-r--r-- 1 root root 15K ... /opt/dolphinscheduler/libs/worker-server/dolphinscheduler-task-lineage-3.2.2.jar
-rw-r--r-- 1 root root 15K ... /opt/dolphinscheduler/libs/master-server/dolphinscheduler-task-lineage-3.2.2.jar
-rw-r--r-- 1 root root 15K ... /opt/dolphinscheduler/libs/api-server/dolphinscheduler-task-lineage-3.2.2.jar
-rw-r--r-- 1 root root 15K ... /opt/dolphinscheduler/libs/standalone-server/dolphinscheduler-task-lineage-3.2.2.jar

==> 3/6 TaskPluginManager 启动日志（插件已被海豚加载）
... TaskPluginManager:[60] - Registering task plugin: LINEAGE - LineageTaskChannelFactory
... TaskPluginManager:[65] - Registered task plugin: LINEAGE - LineageTaskChannelFactory

==> 6/6 端到端
上线响应: {"code": 0, "msg": "success", "data": true, "success": true, "failed": false}
启动响应: {"code": 0, "msg": "success", "data": 184725350915648, ...}
   [0] 工作流实例 2 state=SUCCESS | 任务实例 2 state=SUCCESS taskType=LINEAGE
```

### 5.1 「血缘 + 业务口径 + 完整报告」真实任务日志（来自 `verify_knowledge.py`）

任务 SQL 就是 `examples/knowledge_demo/cdw/dwd_卷烟产量码段明细.sql`，
知识库先用 `python -m lineage.cli kb build` 建好（75 条口径）。
下面是海豚任务实例日志的**真实原文**（`GET /log/detail?taskInstanceId=<id>`；
为便于阅读去掉了海豚自己加的 `[INFO] 时间戳 - ` 前缀）：

```text
╔══════════════════════════════════════════════════════════════════╗
║              数 据 血 缘 分 析 报 告   LINEAGE REPORT            ║
╚══════════════════════════════════════════════════════════════════╝
  分析模式 : SQL 解析   |   方言 : hive   |   语句数 : 1   |   耗时 : 22 ms
  血缘服务 : http://172.17.0.1:18080/analyze

  ┌── ① 表级血缘 ──────────────────────────────────────────────────
  │  数据流向：
  │      ods.ods_卷烟码段流水  ──►  cdw.dwd_卷烟产量码段明细
  │  源表（输入 1 张）: ods.ods_卷烟码段流水
  │  目标表（输出 1 张）: cdw.dwd_卷烟产量码段明细

  ┌── ② 字段级血缘（17 个字段映射）────────────────────────────────
  │  目标表 : cdw.dwd_卷烟产量码段明细
  │  目标字段             │ 来源字段                           │ 加工表达式                                
  │  ──────────────────────┼────────────────────────────────────┼────────────────────────────────────────────
  │  work_order_no        │ ods.ods_卷烟码段流水.work_order_no │ b.work_order_no AS work_order_no
  │  plant_code           │ ods.ods_卷烟码段流水.plant_code    │ b.plant_code AS plant_code
  │  brand_code           │ ods.ods_卷烟码段流水.brand_code    │ b.brand_code AS brand_code
  │  batch_no             │ ods.ods_卷烟码段流水.batch_no      │ b.batch_no AS batch_no
  │  dama_qty_total       │ ods.ods_卷烟码段流水.dama_qty      │ SUM(b.dama_qty) AS dama_qty_total
  │  tiaoma_qty_total     │ ods.ods_卷烟码段流水.tiaoma_qty    │ SUM(b.tiaoma_qty) AS tiaoma_qty_total
  │  chongma_qty_total    │ ods.ods_卷烟码段流水.chongma_qty   │ SUM(b.chongma_qty) AS chongma_qty_total
  │  chanliang_qty        │ ods.ods_卷烟码段流水.chongma_qty   │ SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM…
  │  chanliang_qty        │ ods.ods_卷烟码段流水.dama_qty      │ SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM…
  │  chanliang_qty        │ ods.ods_卷烟码段流水.tiaoma_qty    │ SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM…
  │  dama_rate            │ ods.ods_卷烟码段流水.chongma_qty   │ CASE WHEN SUM(b.dama_qty) + SUM(b.tiaoma_…
  │  dama_rate            │ ods.ods_卷烟码段流水.dama_qty      │ CASE WHEN SUM(b.dama_qty) + SUM(b.tiaoma_…
  │  dama_rate            │ ods.ods_卷烟码段流水.tiaoma_qty    │ CASE WHEN SUM(b.dama_qty) + SUM(b.tiaoma_…
  │  dama_cig_qty         │ ods.ods_卷烟码段流水.dama_qty      │ SUM(b.dama_qty) * 250 AS dama_cig_qty
  │  chanliang_cig        │ ods.ods_卷烟码段流水.chongma_qty   │ (SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SU…
  │  … 其余 2 行见完整报告

  ┌── ③ 加工条件（过滤 / 分区）────────────────────────────────────
  │  语句 1 : b.dt = '2026-01-01'   |   分区 dt = 2026-01-01

  ┌── ④ 加工 SQL 原文 ─────────────────────────────────────────────
  │      INSERT OVERWRITE TABLE cdw.dwd_卷烟产量码段明细 PARTITION(dt = '2026-01-01') SELECT b.work_order_no
  │      AS work_order_no, b.plant_code AS plant_code, b.brand_code AS brand_code, b.batch_no AS batch_no,
  │      SUM(b.dama_qty) AS dama_qty_total, SUM(b.tiaoma_qty) AS tiaoma_qty_total, SUM(b.chongma_qty) AS
  │      chongma_qty_total, SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty) AS chanliang_qty, CASE
  │      WHEN SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty) > 0 THEN ROUND(SUM(b.dama_qty) /
  │      (SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty)), 6) ELSE 0 END AS dama_rate,
  │      SUM(b.dama_qty) * 250 AS dama_cig_qty, (SUM(b.dama_qty) + SUM(b.tiaoma_qty) - SUM(b.chongma_qty)) *
  │      250 AS chanliang_cig FROM ods.ods_卷烟码段流水 AS b WHERE b.dt = '2026-01-01' GROUP BY
  │      b.work_order_no, b.plant_code, b.brand_code, b.batch_no

  ┌── ⑤ 业务口径（知识库匹配）──────────────────────────────────────
  │  ★ 1. 产量（chanliang_qty） = 打码量 + 跳码量 - 重码量
  │       类型 聚合 · 置信度 0.9 · 匹配 目标字段 · 目标表 cdw.dwd_卷烟产量码段明细
  │       摘要 依赖 chongma_qty(重码量), dama_qty(打码量), tiaoma_qty(跳码量) · 链路 cdw.dwd_卷烟产量码段明细 → ods.ods_卷烟码段流水 → src.mes_码段采集接口
  │  ★ 2. 产量（条）（chanliang_cig） = (打码量 + 跳码量 - 重码量) * 250
  │       类型 聚合 · 置信度 0.9 · 匹配 目标字段 · 目标表 cdw.dwd_卷烟产量码段明细
  │       摘要 依赖 chongma_qty(重码量), dama_qty(打码量), tiaoma_qty(跳码量) · 链路 cdw.dwd_卷烟产量码段明细 → ods.ods_卷烟码段流水 → src.mes_码段采集接口
  │  ★ 3. 打码量合计（dama_qty_total） = SUM(打码量)
  │       类型 聚合 · 置信度 0.9 · 匹配 目标字段 · 目标表 cdw.dwd_卷烟产量码段明细
  │       摘要 依赖 dama_qty(打码量) · 链路 cdw.dwd_卷烟产量码段明细 → ods.ods_卷烟码段流水 → src.mes_码段采集接口
  │  … 另有 4 条口径（跳码量合计、重码量合计、打码量（条））详见完整报告 · 口径由 kb build 从加工脚本自动提炼（语法级）
  │  字段中文名 chongma_qty → 重码量 | dama_qty → 打码量 | tiaoma_qty → 跳码量 | chanliang_qty → 产量 | dama_rate → 打码占比 | chanliang_cig → 产量（条）  …（共 14 项，详见报告）
  │  业务规则 3 条 · 示例 [业务规则（注释）] 产量口径：打码量+跳码量-重码量（箱）（脚本注释）

  ══════════════════════════════════════════════════════════════════
  ✅ 血缘分析完成 | 源表 1 张 → 目标表 1 张 | 字段映射 17 个 | 业务口径命中 7 条 | 耗时 22 ms
  📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_20260920_210854_1342968a
     （容器内访问用: http://172.17.0.1:18080/report/rpt_20260920_210854_1342968a）
  ══════════════════════════════════════════════════════════════════

输出参数已写入 varPool: [lineage_mode, lineage_service_url, lineage_input_tables, lineage_output_tables, lineage_input_table_count, lineage_output_table_count, lineage_kb_available, lineage_metric_count, lineage_metric_names, lineage_cost_ms, lineage_report_id, lineage_report_url, lineage_report_raw]
```

> 上面是**完整的一段日志**（从报告框到 varPool 行）：② 段只打了 15 行（`… 其余 2 行见完整报告`），
> ⑤ 段只展开 3 条口径 + 2 行折叠（术语 / 规则），📊 行就是可点击的完整报告地址。
> 复现命令：`bash /usr/local/bin/prep-ds-demo.sh`（恢复演示环境）→
> `.venv/bin/python apps/apps/ds-plugin/verify/verify_knowledge.py`。该脚本会断言这张表里的每一个特征，
> 并**真的去 GET 一次报告 URL**（打印 HTTP 状态 / Content-Type / 字节数 / 表格行数 / 卡片数）：

```text
6) 断言检查
   ✅ ① 表级血缘          ✅ ② 字段级血缘        ✅ ③ 加工条件        ✅ ④ 加工 SQL 原文
   ✅ ⑤ 业务口径          ✅ 产量口径公式原文     ✅ 上游链路          ✅ 字段中文名
   ✅ 汇总行口径命中数     ✅ varPool 新增口径出参
   ✅ ② 字段级是紧凑表格（表头 + 分隔线）
   ✅ ② 超出 15 行折叠成一行提示
   ✅ ⑤ 只展开最关键的 3 条口径
   ✅ ⑤ 其余口径折叠成一行
   ✅ 📊 完整报告 URL 行（可点击）
   ✅ varPool 新增报告出参
最终任务实例 1 状态: SUCCESS，口径命中 7 条
任务日志里的完整报告地址: http://localhost:18080/report/rpt_20260920_210854_1342968a
   ✅ 报告可打开：HTTP 200 text/html; charset=utf-8 30331 字节，字段映射行 17 / 口径卡片 7
```

### 5.2 降级验证：服务端是旧版本（没有 `/analyze`）

`verify_knowledge.py --degraded` 配合 `evidence/p5_legacy_proxy.py`（把 `/analyze` 变成 404、
其余转发真实服务）跑出来的**真实日志**：

```
[WARN] ... 一体化端点不可用（lineage service returned HTTP 404 : {"success": false, "error": "unknown endpoint /analyze（旧版服务）"}
），回退到 http://172.17.0.1:18099/parse：本次只输出血缘，不含业务口径
分析模式   : sql - SQL 血缘解析 + 业务口径匹配 (analyze SQL + knowledge base)
血缘服务   : http://172.17.0.1:18099/analyze
...
血缘服务 : http://172.17.0.1:18099/parse        <-- 报告头显示实际调用的端点
  ┌── ⑤ 业务口径（知识库匹配）──────────────────────────────────────
  │  未匹配到业务口径（可先执行 kb build 建库）
  ✅ 血缘分析完成 | 源表 1 张 → 目标表 1 张 | 字段映射 17 个 | 业务口径命中 0 条 | 耗时 7 ms
```

结论：**任务照样 SUCCESS，①②③④ 段一字不少**，只是第 ⑤ 段退化成一行提示；
因为没有 `report` 段，📊 报告行**整行不打印**（安静略过，不报错）。
`verify_knowledge.py --degraded` 会一并断言「降级路径不输出报告 URL 行」。

### 5.3 `mode=impact` / `mode=upstream` 回归

```bash
.venv/bin/python verify_impact_mode.py   # 下游影响：①②③ 段 + 下游影响段，第 ⑤ 段不出现
```

`impact` / `upstream` 模式走 `/impact` / `/upstream`（没有 `knowledge` 段），
所以插件**不打印**第 ⑤ 段，汇总行也保持原来的「… | 字段映射 N 个 | 耗时 N ms」——
这两条路与改造前完全一致（已实测，见 `evidence/p5_verify_all.sh` 第 4 步）。

## 6. 已知坑 / 限制（踩过的）

1. **海豚 standalone 用内存 H2**（`jdbc:h2:mem:dolphinscheduler`），
   `docker restart ds-standalone` 之后**所有项目、工作流、调度、实例数据都会清空**。
   原有的「烟草数仓演示」项目 + 4 个演示工作流就是这么没的，需要重跑
   `/root/projects/sql-lineage-mvp` 里的
   `.venv/bin/python demos/ds_setup_demo.py --no-export` 重新生成
   （重新生成后 projectCode / workflowCode 会变，因为它们是新 code）。
   插件 jar 在容器文件系统里，`docker restart` 不会丢；`docker rm` 重建容器才会丢。
2. 容器内 `conf/`、`ui/` 的改动是**临时的**——容器重建就没了。源文件都留在本项目
   `conf/`、`ui-static/` 下，重跑 `deploy_ui.sh` 即可恢复。
3. 插件运行在 worker 里，`serviceUrl` 要用**容器内能访问到宿主**的地址
   （本环境是 docker 默认网桥网关 `172.17.0.1`；宿主机上直接访问 `localhost:18080` 是通的，
   但容器不能访问 `localhost`）。
4. UI 端只做到「接口层验证」：`/dynamic/Universal/taskTypes` 已返回 LINEAGE，
   静态表单 JSON 也是 200；**没有做浏览器里的可视化点击验证**（本环境的浏览器工具调用会超时），
   DAG 侧边栏里是否如预期渲染出图标+表单，建议人工打开
   `http://localhost:12345/dolphinscheduler/ui/projects/<projectCode>/workflow/definitions/<wfCode>` 看一眼。
5. 插件没做「取消」支持（`cancel()` 空实现）——它只是一次短 HTTP 调用，没有子进程可杀。
5.1 **报告地址是「宿主机视角」**：日志里的 `http://localhost:18080/report/<id>` 是给 Windows/宿主
   浏览器点的；在容器里（如 `docker exec ds-standalone curl`）要用同一行下面给的
   `http://172.17.0.1:18080/report/<id>`。两者指向同一个服务、同一份文件。
5.2 报告目录 `reports/` 只保留**最近 200 份**（`prune_reports` 自动清理），旧报告被清后
   `GET /report/<id>` 返回 404 HTML 页（不是 JSON），`GET /reports` 可看最近清单。
5.3 `POST /analyze` 走 HTTP 时**默认生成报告**（`with_report` 默认 true，HTTP 层补齐）；
   想让某次调用不落盘就显式传 `with_report=false`。
6. 血缘服务的 `graph` 参数只接受**服务端本地文件**（相对其项目根或绝对路径），
   插件只是把这个字符串透传；想换图要保证服务端能读到该文件。

---

## 7. 第二个任务类型：LINEAGE_DAG（工作流级血缘分析）

`LINEAGE` 看的是**一条 SQL**；`LINEAGE_DAG` 看的是**一整个工作流**。
它挂在工作流**尾部**，运行时自己通过海豚 OpenAPI 把本工作流的**全部任务脚本**拉下来批量解析，
一次给出四样单脚本任务给不出的东西：

1. **全链路图谱**：`src.erp_生产工单明细 → ods.ods_卷烟产量流水 → cdw.dwd_卷烟产量明细 → cdw.dws_产量汇总`
   （工作流内的链路还会用 `warehouse_graph.json` 向上补 `src` / `ods` 层，跨工作流的上下游也标出来）；
2. **跨任务字段血缘**：每条字段映射都带「来源任务」，直接回答「这个指标是哪个节点算出来的、上游字段谁产的」；
3. **工作流级业务口径汇总**：整个工作流产出的指标口径一次对齐（默认展开最关键的 3 条，其余折叠）；
4. **链路质量体检**：断链（产出表无人消费）/ 孤岛（输入表无上游）/ 环路 / 产出表未登记口径 ——
   每一条都会再和全局血缘核对一次，标出「其实只是跨工作流」的情况。

### 7.1 为什么挂在尾部：历史工作流零改造

* 插件**不从执行结果里拿数据**，只读工作流的**定义**（`taskDefinitionList`）。所以：
  * 原有 N 个 SQL 任务**一行都不用改**，不需要填任何参数、不需要写库名表名；
  * 工作流身份来自 `TaskExecutionContext`（`getProjectCode()` / `getProcessDefineCode()`），
    插件自动带在请求体里 —— 用户唯一要做的事就是「拖一个 LINEAGE_DAG 节点到画布最后」。
* 挂尾部是**语义上更自然**（跑完再体检），但**技术上不依赖上游是否成功**：它读的是定义，
  挂哪儿结果都一样。本演示环境没有真实 Hive 数据源，SQL 任务必然 FAILURE，而海豚**不会提交
  失败上游的下游任务** —— 所以验证脚本里同时给了「尾部串联」与「同层挂载」两种接法
  （见 7.6 的说明），后者能在没有可用数据源的环境里把节点真跑起来。

### 7.2 任务参数（taskParams）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `scope` | string | `current` | `current`=只分析本工作流；`project`=把项目下**所有**工作流一起分析（跨工作流链路） |
| `taskTypes` | string | `SQL,SHELL,PYTHON` | 解析哪些任务类型的脚本，逗号分隔；不在其中的任务仍会列进任务清单（标记为跳过） |
| `includeSubProcess` | bool | `false` | 是否递归展开 `SUB_PROCESS` / `DEPENDENT` 引用的工作流里的任务（最多 3 层） |
| `dialect` | string | `hive` | 传给解析器的 SQL 方言 |
| `serviceUrl` | string | `http://172.17.0.1:18080` | 血缘服务地址（容器内访问宿主的 docker 网关地址） |
| `timeout` | int | `60000` | HTTP 超时（毫秒）；工作流级要多拉几个脚本，默认比 LINEAGE 大一倍 |

**没有 SQL 输入框** —— 这是它和 `LINEAGE` 最大的界面差异（参数类 `LineageDagParameters` 里也确实没有 `sql` 字段）。

> **节点设置弹窗只暴露 `scope` / `taskTypes` / `includeSubProcess` / `serviceUrl` 四项**
> （外加「节点名称/描述/超时/前置任务」等通用段，超时走通用段的 `timeout` 字段）。
> `dialect` 与 `timeout` 只在参数类里保留默认值、不在表单上；`dialect` 目前固定 `hive`
> —— 后续要支持 Shell/Python 脚本时再决定是否放开。
> 表单实现与验证见 **7.8**。

### 7.3 运行流程

```text
海豚工作流（原有 N 个任务一行不改）
   │
   ├─ t_sql_ods_产量流水 ──┐
   ├─ t_sql_dwd_产量明细 ──┤   （业务脚本，不感知血缘插件）
   ├─ t_sql_dws_产量汇总 ──┘
   │
   └─ t_dag_工作流血缘（LINEAGE_DAG，尾节点）
          │  ① GET  /projects/{pc}/process-definition/{code}   ← 拉工作流定义（登录后带 sessionId）
          │  ② 提取脚本：SQL→taskParams.sql / SHELL·PYTHON→taskParams.rawScript / pre·postStatements
          │  ③ 逐个脚本本地解析（SqlLineageParser，方言 hive）+ 知识库 match_knowledge
          │  ④ 合并成工作流级血缘（表级边 / 任务级依赖 / 跨任务字段血缘）
          │  ⑤ 链路质量体检（断链·孤岛·环路·未登记口径，并与全局血缘核对）
          ▼
   POST /analyze-workflow（宿主 172.17.0.1:18080）
          │
          ├─ 返回 JSON（任务清单 / merged.table_lineage / chain / quality / knowledge）
          └─ 落一份工作流级 HTML 报告 → 日志末尾打印可点击 URL
```

### 7.4 任务日志格式（五段式，风格与 LINEAGE 一致）

```text
╔══════════════════════════════════════════════════════════════════╗
║           工 作 流 血 缘 分 析 报 告   WORKFLOW LINEAGE REPORT   ║
╚══════════════════════════════════════════════════════════════════╝
  工作流   : wf_xxx   任务 N 个（解析 M）   语句 S   耗时 T ms
  ┌── ① 任务清单       任务名 │ 类型 │ 脚本长度 │ 语句 │ 口径（+ 每条任务的产出表）
  ┌── ② 全链路图谱     链路分行（超宽自动折行）＋ 分层 ＋ 任务级依赖（A ──► B，经哪张表）
  ┌── ③ 跨任务字段血缘  来源任务 │ 目标字段 │ 来源字段 │ 加工表达式（最多 15 行，其余折叠）
  ┌── ④ 业务口径汇总    ★ 展开 3 条（公式 + 目标表/类型/置信度/匹配方式），其余折叠
  ┌── ⑤ 链路质量体检    ✅ 无环路 · ⚠ N 张产出表无人消费 · ✅ 无孤岛输入 · ⚠ N 张表未登记口径
  ✅ 工作流血缘分析完成 | 任务 N 个 | 节点 N | 边 N | 字段映射 N 个 | 口径命中 N 条 | 耗时 T ms
  📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_xxx
```

出参（`varPool`，下游任务可直接引用）：

```text
lineage_dag_workflow / lineage_dag_workflow_code / lineage_dag_task_count /
lineage_dag_parsed_task_count / lineage_dag_node_count / lineage_dag_edge_count /
lineage_dag_metric_count / lineage_dag_column_count / lineage_dag_chain /
lineage_dag_service_url / lineage_dag_cost_ms / lineage_dag_report_id /
lineage_dag_report_url / lineage_dag_report_raw
```

### 7.5 真实任务日志（`verify_dag.py` 跑出来的原文，去掉了海豚的 `[INFO] 时间戳 - ` 前缀）

```text
╔══════════════════════════════════════════════════════════════════╗
║           工 作 流 血 缘 分 析 报 告   WORKFLOW LINEAGE REPORT   ║
╚══════════════════════════════════════════════════════════════════╝
  工作流   : wf_dag_工作流血缘演示_并行运行   任务 4 个（解析 3）   语句 3   耗时 25 ms
  血缘服务 : http://172.17.0.1:18080/analyze-workflow   （口径命中 7 条 / 节点 6 / 边 5）

  ┌── ① 任务清单（4 个）─────────────────────────────────────────────
  │  任务名             │ 类型         │ 脚本长度 │ 语句 │ 口径
  │  ────────────────────┼──────────────┼──────────┼──────┼──────
  │  t_sql_ods_产量流水 │ SQL          │ 458      │ 1    │ 2
  │      └─ 产出 : ods.ods_卷烟产量流水
  │  t_sql_dwd_产量明细 │ SQL          │ 471      │ 1    │ 2
  │      └─ 产出 : cdw.dwd_卷烟产量明细
  │  t_sql_dws_产量汇总 │ SQL          │ 383      │ 1    │ 3
  │      └─ 产出 : cdw.dws_产量汇总
  │  t_dag_工作流血缘   │ LINEAGE_DAG  │ 0        │ 0    │ 0
  │      ⚠ 跳过：任务类型 LINEAGE_DAG 不在本次解析范围（PYTHON,SHELL,SQL）

  ┌── ② 全链路图谱（跨任务）────────────────────────────────────────
  │  链路（4 级）:
  │      src.erp_生产工单明细 ──► ods.ods_卷烟产量流水 ──► cdw.dwd_卷烟产量明细 ──►
  │      cdw.dws_产量汇总
  │  分层 : src → ods → cdw（3 层；本工作流内 4 级）
  │  任务级依赖（2 条）:
  │      t_sql_dwd_产量明细 ──► t_sql_dws_产量汇总   （经 cdw.dwd_卷烟产量明细）
  │      t_sql_ods_产量流水 ──► t_sql_dwd_产量明细   （经 ods.ods_卷烟产量流水）

  ┌── ③ 跨任务字段血缘（14 个字段映射）──────────────────────────────
  │  来源任务           │ 目标字段           │ 来源字段                       │ 加工表达式
  │  ────────────────────┼────────────────────┼────────────────────────────────┼────────────────────────────────────
  │  t_sql_ods_产量流水 │ work_order_no      │ src.erp_生产工单明细.work_ord… │ a.work_order_no AS work_order_no
  │  t_sql_ods_产量流水 │ plant_code         │ src.erp_生产工单明细.plant_co… │ a.plant_code AS plant_code
  │  t_sql_ods_产量流水 │ plant_name         │ dim.dim_plant.plant_name       │ p.plant_name AS plant_name
  │  t_sql_ods_产量流水 │ qty                │ src.erp_生产工单明细.qty       │ a.qty AS qty
  │  t_sql_ods_产量流水 │ dt                 │ src.erp_生产工单明细.dt        │ a.dt AS dt
  │  t_sql_dwd_产量明细 │ work_order_no      │ ods.ods_卷烟产量流水.work_ord… │ a.work_order_no AS work_order_no
  │  t_sql_dwd_产量明细 │ plant_code         │ ods.ods_卷烟产量流水.plant_co… │ a.plant_code AS plant_code
  │  t_sql_dwd_产量明细 │ brand_name         │ dim.dim_brand.brand_name       │ b.brand_name AS brand_name
  │  t_sql_dwd_产量明细 │ chanliang_qty      │ ods.ods_卷烟产量流水.qty       │ a.qty AS chanliang_qty
  │  t_sql_dwd_产量明细 │ dt                 │ ods.ods_卷烟产量流水.dt        │ a.dt AS dt
  │  t_sql_dws_产量汇总 │ plant_code         │ cdw.dwd_卷烟产量明细.plant_co… │ d.plant_code AS plant_code
  │  t_sql_dws_产量汇总 │ chanliang_qty      │ cdw.dwd_卷烟产量明细.chanlian… │ SUM(d.chanliang_qty) AS chanliang…
  │  t_sql_dws_产量汇总 │ work_order_cnt     │ cdw.dwd_卷烟产量明细.work_ord… │ COUNT(DISTINCT d.work_order_no) A…
  │  t_sql_dws_产量汇总 │ dt                 │ cdw.dwd_卷烟产量明细.dt        │ d.dt AS dt

  ┌── ④ 业务口径汇总（知识库匹配）──────────────────────────────────
  │  ★ 1. 工单数（work_order_cnt） = COUNT(1)
  │       目标表 cdw.dws_产量汇总 · 类型 聚合 · 置信度 0.9 · 匹配 目标字段
  │  ★ 2. 不良品率（defect_rate） = CASE WHEN 产量 > 0 THEN ROUND(不良品量 / 产量, 6) ELSE 0 END
  │       目标表 cdw.dwd_卷烟产量明细 · 类型 条件分支 · 置信度 0.85 · 匹配 输出表
  │  ★ 3. 产量（条）（output_qty_cig） = 产量 * 250
  │       目标表 cdw.dwd_卷烟产量明细 · 类型 算术计算 · 置信度 0.9 · 匹配 输出表
  │  … 另有 4 条口径详见完整报告（口径由 kb build 从加工脚本自动提炼）

  ┌── ⑤ 链路质量体检 ───────────────────────────────────────────────
  │  ✅ 环路：任务间未形成环，表级依赖亦无环
  │  ⚠ 1 张产出表在本工作流内无人消费（断链）
  │       cdw.dws_产量汇总  ← 本工作流内无人消费；全局血缘显示下游在其它工作流：cdw.dws_产销存汇总, cdw…
  │  ✅ 0 张输入表在本工作流内无上游（无孤岛输入）
  │  ✅ 0 张产出表未在知识库登记口径（口径已全登记）

  ══════════════════════════════════════════════════════════════════
  ✅ 工作流血缘分析完成 | 任务 4 个 | 节点 6 | 边 5 | 字段映射 14 个 | 口径命中 7 条 | 耗时 25 ms
  📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_20260920_215543_4079296f
     （容器内访问用: http://172.17.0.1:18080/report/rpt_20260920_215543_4079296f）
  ══════════════════════════════════════════════════════════════════

输出参数已写入 varPool: [lineage_dag_workflow, lineage_dag_workflow_code, lineage_dag_task_count, lineage_dag_parsed_task_count, lineage_dag_node_count, lineage_dag_edge_count, lineage_dag_metric_count, lineage_dag_column_count, lineage_dag_chain, lineage_dag_service_url, lineage_dag_cost_ms, lineage_dag_report_id, lineage_dag_report_url, lineage_dag_report_raw]
```

同一份能力作用在**历史工作流**上（给 `wf_dws_汇总` 追加一个尾节点，原有 4 个 SQL 任务一行未改）：

```text
╔══════════════════════════════════════════════════════════════════╗
║           工 作 流 血 缘 分 析 报 告   WORKFLOW LINEAGE REPORT   ║
╚══════════════════════════════════════════════════════════════════╝
  工作流   : wf_dws_汇总   任务 5 个（解析 4）   语句 4   耗时 31 ms
  血缘服务 : http://172.17.0.1:18080/analyze-workflow   （口径命中 14 条 / 节点 8 / 边 7）

  ┌── ① 任务清单（5 个）─────────────────────────────────────────────
  │  任务名             │ 类型         │ 脚本长度 │ 语句 │ 口径
  │  ────────────────────┼──────────────┼──────────┼──────┼──────
  │  t_dws_产量汇总     │ SQL          │ 484      │ 1    │ 3
  │      └─ 产出 : cdw.dws_产量汇总
  │  t_dws_库存汇总     │ SQL          │ 477      │ 1    │ 3
  │      └─ 产出 : cdw.dws_库存汇总
  │  t_dws_税利汇总     │ SQL          │ 906      │ 1    │ 5
  │      └─ 产出 : cdw.dws_税利汇总
  │  t_dws_产销存汇总   │ SQL          │ 1017     │ 1    │ 3
  │      └─ 产出 : cdw.dws_产销存汇总
  │  t_dag_工作流血缘   │ LINEAGE_DAG  │ 0        │ 0    │ 0
  │      ⚠ 跳过：任务类型 LINEAGE_DAG 不在本次解析范围（PYTHON,SHELL,SQL）

  ┌── ② 全链路图谱（跨任务）────────────────────────────────────────
  │  链路（5 级）:
  │      src.erp_生产工单明细 ──► ods.ods_卷烟产量流水 ──► cdw.dwd_卷烟产量明细 ──►
  │      cdw.dws_产量汇总 ──► cdw.dws_税利汇总
  │  分层 : src → ods → cdw（3 层；本工作流内 3 级）
  │  跨工作流上游 : src.erp_生产工单明细, ods.ods_卷烟产量流水  （全局血缘拼接）
  │  任务级依赖（3 条）:
  │      t_dws_产量汇总 ──► t_dws_产销存汇总   （经 cdw.dws_产量汇总）
  │      t_dws_产量汇总 ──► t_dws_税利汇总   （经 cdw.dws_产量汇总）
  │      t_dws_库存汇总 ──► t_dws_产销存汇总   （经 cdw.dws_库存汇总）

  ┌── ③ 跨任务字段血缘（25 个字段映射）──────────────────────────────
  │  来源任务           │ 目标字段           │ 来源字段                       │ 加工表达式
  │  ────────────────────┼────────────────────┼────────────────────────────────┼────────────────────────────────────
  │  t_dws_产量汇总     │ plant_code         │ cdw.dwd_卷烟产量明细.plant_co… │ d.plant_code AS plant_code
  │  t_dws_产量汇总     │ brand_code         │ cdw.dwd_卷烟产量明细.brand_co… │ d.brand_code AS brand_code
  │  t_dws_产量汇总     │ total_output_qty   │ cdw.dwd_卷烟产量明细.output_q… │ SUM(d.output_qty) AS total_output…
  │  t_dws_产量汇总     │ total_defect_qty   │ cdw.dwd_卷烟产量明细.defect_q… │ SUM(d.defect_qty) AS total_defect…
  │  t_dws_产量汇总     │ work_order_cnt     │ (常量)                         │ COUNT(1) AS work_order_cnt
  │  t_dws_库存汇总     │ plant_code         │ cdw.dwd_成品库存明细.plant_co… │ k.plant_code AS plant_code
  │  t_dws_库存汇总     │ brand_code         │ cdw.dwd_成品库存明细.brand_co… │ k.brand_code AS brand_code
  │  t_dws_库存汇总     │ total_stock_qty    │ cdw.dwd_成品库存明细.stock_qty │ SUM(k.stock_qty) AS total_stock_q…
  │  t_dws_库存汇总     │ total_stock_amt    │ cdw.dwd_成品库存明细.stock_amt │ SUM(k.stock_amt) AS total_stock_a…
  │  t_dws_库存汇总     │ batch_cnt          │ cdw.dwd_成品库存明细.batch_no  │ COUNT(DISTINCT k.batch_no) AS bat…
  │  t_dws_税利汇总     │ plant_code         │ cdw.dwd_税利明细.plant_code    │ t.plant_code AS plant_code
  │  t_dws_税利汇总     │ brand_code         │ cdw.dwd_税利明细.brand_code    │ t.brand_code AS brand_code
  │  t_dws_税利汇总     │ stat_month         │ cdw.dwd_税利明细.stat_month    │ t.stat_month AS stat_month
  │  t_dws_税利汇总     │ total_tax_amt      │ cdw.dwd_税利明细.tax_amt       │ SUM(t.tax_amt) AS total_tax_amt
  │  t_dws_税利汇总     │ total_profit_amt   │ cdw.dwd_税利明细.profit_amt    │ SUM(t.profit_amt) AS total_profit…
  │  … 其余 10 行见完整报告

  ┌── ④ 业务口径汇总（知识库匹配）──────────────────────────────────
  │  ★ 1. 单箱税利（tax_profit_per_box） = CASE WHEN MAX(总产量) > 0 THEN ROUND(SUM(税利总额) / MAX(总产量), 2) ELSE 0 END
  │       目标表 cdw.dws_税利汇总 · 类型 条件分支 · 置信度 0.85 · 匹配 目标字段
  │  ★ 2. 总产量（total_output_qty） = SUM(产量)
  │       目标表 cdw.dws_产量汇总 · 类型 聚合 · 置信度 0.9 · 匹配 目标字段
  │  ★ 3. 不良品总量（total_defect_qty） = SUM(不良品量)
  │       目标表 cdw.dws_产量汇总 · 类型 聚合 · 置信度 0.9 · 匹配 目标字段
  │  … 另有 11 条口径详见完整报告（口径由 kb build 从加工脚本自动提炼）

  ┌── ⑤ 链路质量体检 ───────────────────────────────────────────────
  │  ✅ 环路：任务间未形成环，表级依赖亦无环
  │  ⚠ 2 张产出表在本工作流内无人消费（断链）
  │       cdw.dws_产销存汇总  ← 本工作流内无人消费；全局血缘显示下游在其它工作流：ads.ads_产销存月报, ads…
  │       cdw.dws_税利汇总  ← 本工作流内无人消费；全局血缘显示下游在其它工作流：ads.ads_税利分析, ads.a…
  │  ⚠ 4 张输入表在本工作流内无上游（孤岛输入）
  │       cdw.dwd_卷烟产量明细  ← 本工作流内无上游；全局血缘显示上游在其它工作流：dim.dim_brand, dim.dim_pl…
  │       cdw.dwd_卷烟销量明细  ← 本工作流内无上游；全局血缘显示上游在其它工作流：dim.dim_brand, ods.ods_卷…
  │       cdw.dwd_成品库存明细  ← 本工作流内无上游；全局血缘显示上游在其它工作流：dim.dim_plant, ods.ods_成…
  │       cdw.dwd_税利明细  ← 本工作流内无上游；全局血缘显示上游在其它工作流：dim.dim_brand, ods.ods_税…
  │  ✅ 0 张产出表未在知识库登记口径（口径已全登记）

  ══════════════════════════════════════════════════════════════════
  ✅ 工作流血缘分析完成 | 任务 5 个 | 节点 8 | 边 7 | 字段映射 25 个 | 口径命中 14 条 | 耗时 31 ms
  📊 完整报告（浏览器打开）: http://localhost:18080/report/rpt_20260920_215546_daa91d7c
     （容器内访问用: http://172.17.0.1:18080/report/rpt_20260920_215546_daa91d7c）
  ══════════════════════════════════════════════════════════════════

输出参数已写入 varPool: [lineage_dag_workflow, lineage_dag_workflow_code, lineage_dag_task_count, lineage_dag_parsed_task_count, lineage_dag_node_count, lineage_dag_edge_count, lineage_dag_metric_count, lineage_dag_column_count, lineage_dag_chain, lineage_dag_service_url, lineage_dag_cost_ms, lineage_dag_report_id, lineage_dag_report_url, lineage_dag_report_raw]
```

### 7.6 编译、部署与运行

```bash
cd /root/projects/sql-lineage-mvp/ds-plugin
bash build.sh                    # 导出依赖 → javac --release 8 → 打包 → 拷进容器 4 个 libs → 重启海豚
# 插件注册日志里必须出现两行（缺一个都不行）
#   Registered task plugin: LINEAGE_DAG - LineageDagTaskChannelFactory
#   Registered task plugin: LINEAGE - LineageTaskChannelFactory

../.venv/bin/python verify_dag.py            # 端到端：建流 → 上线 → 运行 → 拉 LINEAGE_DAG 日志全文
../.venv/bin/python verify_dag.py --phase1-only
```

`verify_dag.py` 做三件事，全部打真实 HTTP：

| 阶段 | 做什么 | 期望结果 |
|---|---|---|
| ① | 建 `wf_dag_工作流血缘演示`：3 个 SQL 任务**串联** + LINEAGE_DAG **尾节点**（sql3→dag） | 定义回读能看到 4 个任务，`taskType=LINEAGE_DAG` 落库；运行时 SQL 任务因无数据源 FAILURE，海豚不提交下游尾节点（**海豚的依赖语义**，与插件无关） |
| ② | 同样 3 个 SQL + LINEAGE_DAG（**与 SQL 同层**） | LINEAGE_DAG **SUCCESS**，打印日志全文（上面 7.5 就是这一段） |
| ③ | 给已有的 `wf_dws_汇总` **追加**一个 LINEAGE_DAG（save-single） | 回读 5 个任务（4 个原任务 + 1 个新节点），原任务一行未改；DAG 节点 SUCCESS |

> **关于「尾部」**：生产环境数据源可用时，把节点拖到画布最后即可（阶段① 的结构）。
> 本演示环境没有真实 Hive，SQL 任务必然失败，而海豚不会提交失败上游的下游任务，
> 所以阶段②/③ 用「同层挂载」把节点真跑起来 —— 插件读的是工作流**定义**，
> 两种接法解析出的报告**完全一致**（阶段③ 的报告就是 `wf_dws_汇总` 那 4 个真实 SQL 任务）。

### 7.7 让 UI 也能选到「工作流血缘」

`LINEAGE` 是 `LINEAGE_DAG` 的现成样板，两者做法完全一致（细节见第 3 节「让 UI 也能选到血缘分析」）：

1. `conf/dynamic-task-type-config.yaml` 追加一行（图标 + 动态表单 JSON 路径）：

   ```yaml
   - {name: LINEAGE_DAG,icon: /ui/static/lineage/lineage-dag-icon.svg,hover: /ui/static/lineage/lineage-dag-hover.svg,json: /ui/static/lineage/lineage-dag.json}
   ```

2. `ui-static/lineage/lineage-dag.json`：动态任务类型的**声明式**表单元数据（分析范围 / 任务类型 /
   子工作流 / 服务地址四项，与下面 `LDF` 保持一致）。
   ⚠️ 实测：海豚 3.2.2 的 UI 里**没有**消费这份 JSON 的表单渲染器（全量 bundle 中 `formType`
   只出现在 Monaco 的 TypeScript worker 里），侧边栏只取 `name/icon/hover`。所以
   **真正决定「节点设置弹窗长什么样」的是下一项的前端 bundle 补丁**，这份 JSON 只是元数据兑齐。
3. 前端 bundle 打 4 处补丁（源文件归档在 `ui-static/assets/`，`prep_ds_demo.sh` 会自动部署）：

   | 文件 | 补什么 | 作用 |
   |---|---|---|
   | `dag-sidebar.*.js` | `t.dataList.push({taskType:"LINEAGE_DAG",taskCategory:"Universal",…})` | **侧边栏出现「工作流血缘」**（海豚把类型表写死在前端，不 push 就看不见） |
   | `task-type.*.js` | `,LINEAGE_DAG:{alias:"LINEAGE_DAG",helperLinkDisable:!0}` | 类型映射（拖拽 / 画布节点渲染） |
   | `detail-modal.*.js` | ① `,LINEAGE:{…},LINEAGE_DAG:{…}`（节点详情弹窗的类型表）<br>② 保存白名单补一行 `e.taskType==="LINEAGE_DAG"&&(r.scope=…,…)` | 类型表 + **让弹窗填的值真的存进 taskParams**（详见 7.8.1） |
   | `detail.*.js` | 插入 `function LDF({projectCode,from,readonly,data}){…}` + `,LINEAGE_DAG:LDF}` | 节点设置弹窗用 **LINEAGE_DAG 专属表单**（详见 7.8）。**修复前**这里是 `,LINEAGE_DAG:Rr}`，即直接复用 SQL 类型表单，会渲染出 SQL 语句 / 数据源类型 / 数据源实例 / SQL 类型那一套字段 |

部署后在浏览器里 **Ctrl+Shift+R 硬刷新**（文件名带 hash，强缓存）。

### 7.8 LINEAGE_DAG 的专属节点设置表单（`LDF`）

**问题（实测反馈）**：把「工作流血缘」拖到画布上、打开节点设置弹窗，看到的是**和 SQL 类型一模一样**的
表单 —— SQL 语句文本框、数据源类型、数据源实例、SQL 类型。语义完全不对：`LINEAGE_DAG` 的脚本不是
用户填的，而是运行时按工作流定义从海豚 OpenAPI 拉下来的，**根本没有「写 SQL、选数据源」这件事**。

**根因**：海豚前端把「任务类型 → 表单构造函数」的映射表写死在 `ui/assets/detail.<hash>.js` 里：

```js
const oa={SHELL:Sr,SUB_PROCESS:Er,DYNAMIC:na,PYTHON:kr,…,REMOTESHELL:aa,LINEAGE:Rr,LINEAGE_DAG:Rr};
```

`kn`（节点设置组件）在 `setup` 里执行 `oa[data.taskType]({projectCode,from,readonly,data})`，拿到
`{json:[组件描述…], model:参数模型}`，再交给 `get-elements-by-json` 的 `We(json, model)` 渲染。
`Rr` 是 **SQL 类型**的表单函数，`LINEAGE_DAG` 早期为了「拖出来弹窗不报错」直接指向了它 —— 于是
SQL/数据源字段就冒出来了。

**为什么不能改前端源码**：插件仓库只有编译产物（`ui/` 下的静态 bundle），没有海豚前端的源码工程与
构建链；而且部署到容器里生效的就是这份 bundle。所以只能**在 bundle 里补一个专属表单函数**，
做法与前面 4 处补丁一致，源文件归档在 `ui-static/assets/detail.f1164795.js`。

**表单设计**（只留 4 个业务字段 + 通用段，全部对齐 `LineageDagParameters`）：

| 字段 | 组件 | 默认值 | 说明 |
|---|---|---|---|
| `scope` | 下拉 `select` | `current` | `current`=只解析当前工作流；`project`=整个项目 |
| `taskTypes` | 输入框 `input` | `SQL,SHELL,PYTHON` | 解析哪些任务类型的脚本，逗号分隔 |
| `includeSubProcess` | 开关 `switch` | `false` | 是否递归展开 SUB_PROCESS 子流程里的任务 |
| `serviceUrl` | 输入框 `input` | `http://172.17.0.1:18080` | 血缘服务地址 |

外加一条 `type:"custom"` 的**提示文本**：「LINEAGE_DAG · 工作流级血缘：无需填写 SQL / 数据源，
任务运行时自动拉取工作流内的任务脚本并解析……」。

**绝对不能出现**：`field:"sql"`（SQL 语句编辑器 `type:"editor"`）、`field:"type"`（数据源类型）、
`field:"datasource"`（数据源实例）、`field:"sqlType"`（SQL 类型），以及 `displayRows / connParams /
preStatements / postStatements / udfs`。

**通用段**（节点名称 / 执行标志 / 描述 / 任务优先级 / Worker 分组 / 环境 / 任务组 / 超时 / 前置任务）
直接沿用 **SUB_PROCESS（`Er`）** 的那一段 —— 子流程的语义（「挂到工作流里跑」）与工作流级血缘最接近，
它的 json 数组里没有 SQL 专属元件，是最干净的模板：

```js
function LDF({projectCode:e,from:t=0,readonly:n,data:a}){
  const r=h({taskType:"LINEAGE_DAG",name:"",flag:"YES",…,timeout:30,timeoutNotifyStrategy:["WARN"],
            scope:"current",taskTypes:"SQL,SHELL,PYTHON",includeSubProcess:!1,
            serviceUrl:"http://172.17.0.1:18080"});
  return{json:[k(t),                      // 通用段：节点名称
                ...O({projectCode:e,from:t,readonly:n,data:a,model:r}),  // 通用段：编辑态的资源配置
                L(),N(),S(),E(e),w(r,!(a!=null&&a.id)),   // 执行标志/描述/优先级/Worker/环境
                ...R(r,e),...A(r),      // 任务组 / 超时
                /* ↓↓↓ 专属业务字段 ↓↓↓ */
                {type:"custom",field:"lineageDagTips",span:24,widget:B("div",{style:{…}},"…无需填写 SQL…")},
                {type:"select",field:"scope",span:12,name:"解析范围 (scope)",options:[
                    {label:"当前工作流（current）",value:"current"},
                    {label:"整个项目（project）",value:"project"}],
                 validate:{trigger:["input","blur"],required:!0}},
                {type:"input",field:"taskTypes",span:12,name:"解析任务类型 (taskTypes)",
                 props:{placeholder:"SQL,SHELL,PYTHON",maxLength:200},
                 validate:{trigger:["input","blur"],required:!0,message:"…"}},
                {type:"switch",field:"includeSubProcess",span:12,name:"包含子工作流 (includeSubProcess)"},
                {type:"input",field:"serviceUrl",span:12,name:"血缘服务地址 (serviceUrl)",
                 props:{placeholder:"http://172.17.0.1:18080",maxLength:200},
                 validate:{trigger:["input","blur"],required:!0,message:"…"}},
                /* ↑↑↑ 专属业务字段 ↑↑↑ */
                P()}],                 // 通用段：前置任务
         model:r}
}
```

组件描述对象的写法是从同文件里现成字段**反推仿真**的（`type` + `field` + `name`(label) + `span` +
`props` + `options` + `validate`），字段语义在 `get-elements-by-json.<hash>.js` 里可查：
`input/select/switch/checkbox/input-number/editor/custom/…` → `s(Input/Select/Switch/…,{...props,
value:model[field],onUpdateValue})`；`type:"custom"` 的 `widget` 直接就是要渲染的 vnode。
改完与**镜像原版**做逐字符 diff：只有 2 处差异 —— ① 插入 `function LDF(...)`，② `LINEAGE_DAG:` 的值
`Rr`→`LDF`（`LINEAGE:Rr` 保持不动，单脚本血缘的表单一点没变）。

#### 7.8.1 光有表单还不够：保存链路也少了一行（同一批补丁，实测发现）

改完表单后顺手核了一下「点保存会发生什么」，发现**更隐蔽的一处问题**：`detail-modal.<hash>.js` 里的
保存函数 `Ne(model)` 用一串**类型白名单**决定哪些字段进 `taskParams`：

```js
const r={};                                              // r 就是类型专属 taskParams
if((e.taskType==="SUB_PROCESS"||e.taskType==="DYNAMIC")&&(r.processDefinitionCode=…))…
e.taskType==="JAVA"&&(r.runType=e.runType,…)…
…                                                        // 共 25 个分支
e.taskType==="DYNAMIC"&&(r.processDefinitionCode=…,r.listParameters=e.listParameters);
…
taskParams:{localParams,initScript,rawScript,resourceList:[],…r}
```

**原版没有 LINEAGE / LINEAGE_DAG 分支** → 这两个类型保存时 `r` 恒为 `{}`，类型参数被整包丢掉。
用真实的 `Ne()` 真跑一遍（`verify_save_params.py`，输入就是 `LDF` 产出的 model）：

```text
原版 Ne()   → taskParams 键 = [localParams,initScript,rawScript,resourceList]
              值 = {"localParams":[],"resourceList":[]}          ← scope/taskTypes/includeSubProcess/serviceUrl 全丢
补丁后 Ne() → taskParams 键 = […,scope,taskTypes,includeSubProcess,serviceUrl]
              值 = {…,"scope":"current","taskTypes":"SQL,SHELL,PYTHON","includeSubProcess":false,
                    "serviceUrl":"http://172.17.0.1:18080"}
```

也就是说：**只改表单的话，用户填的 4 个值一个都存不下去**，任务虽然能跑（`LineageDagParameters`
里 4 个字段都有 Java 默认值、`checkParameters()` 只校验 serviceUrl 非空），但全是默认值。
所以补丁里加了对应的一行（见 7.7 的表格），映射的字段与表单、`LineageDagParameters` 三者严格对齐：

```js
,e.taskType==="LINEAGE_DAG"&&(r.scope=e.scope,r.taskTypes=e.taskTypes,
                              r.includeSubProcess=e.includeSubProcess,r.serviceUrl=e.serviceUrl)
```

> **故意不映射 `timeout`**：表单里的 `timeout` 是通用段的「任务超时（分钟，默认 30）」，
> 而 `LineageDagParameters.timeout` 是「HTTP 超时（毫秒，默认 60000）」，语义不同；
> 直接映射会把 HTTP 超时设成 30ms。`dialect` 同理保持 Java 默认值（表单未暴露）。
>
> **LINEAGE（单脚本）有同样的问题**（`mode/sql/dialect/serviceUrl` 也会被丢，而
> `LineageParameters.checkParameters()` 要求 sql 非空 ⇒ 该类型从 UI 保存后跑不起来）。
> 这属于本次改动范围之外的历史问题，**没有动它**（演示脚本里的 LINEAGE 任务是走 API 建的，不受影响），
> 如实记在这里，见 7.10。

**四条可执行验证**（都不需要浏览器）：

```bash
# ① 语法：把 ESM 去掉 import 后整文件丢给 QuickJS 编译（new Function 也能过）
.venv/bin/python apps/apps/ds-plugin/verify/verify_ldf_form.py            # ②「渲染函数真跑」：抽 LDF 原样执行 + 真跑渲染管线
.venv/bin/python apps/apps/ds-plugin/verify/verify_ldf_form.py --control  #   对照实验：同一套断言跑 SQL 表单 Rr，必须大面积 FAIL
.venv/bin/python apps/apps/ds-plugin/verify/verify_save_params.py         # ③「表单→保存 payload」：LDF 的 model 喂给真实 Ne()
                                                         #   原版丢 4 个字段 / 补丁后 4 个字段齐全 + 改值能生效
.venv/bin/python apps/apps/ds-plugin/verify/verify_ui_deploy.py           # ④ 服务端 4 个 bundle 200 + .gz 副本 + 内容断言
```

`verify_ldf_form.py` 的做法：从归档 bundle 里把 `LDF` 与它真实调用的 `k/O/L/N/R/A/P`（同文件）、
`S/E/w`（`index.module.*.js`，真实实现，仅改名）、渲染管线 `We`（`get-elements-by-json.*.js`，原样）
一起抽出来，只 mock 各模块 `import` 进来的 Vue/i18n/api 原语，然后在 QuickJS 里真跑。
原版依赖文件会自动从基础镜像 `apache/dolphinscheduler-standalone-server:3.2.2` 抽到
`build/stock_ui/`（已缓存，离线可重复跑）。真实输出：

```text
json 元素数 = 17
[ 0] type=input | field=name            | label=节点名称            | required
[ 8] type=switch | field=timeoutFlag     | label=超时告警
[11] type=custom | field=lineageDagTips  | label=(无)
[12] type=select | field=scope           | label=解析范围 (scope)    | required
[13] type=input  | field=taskTypes       | label=解析任务类型 (taskTypes) | required
[14] type=switch | field=includeSubProcess | label=包含子工作流 (includeSubProcess)
[15] type=input  | field=serviceUrl      | label=血缘服务地址 (serviceUrl) | required
[16] type=select | field=preTasks        | label=前置任务
PASS  json 里不含 SQL/数据源专属字段 (sql/sqlType/datasource/type/displayRows/connParams/…) 实际命中=[]
PASS  json 里不含 SQL 编辑器组件 (type='editor')
PASS  model.scope 默认值 = current / model.taskTypes 默认值 = SQL,SHELL,PYTHON
PASS  model.includeSubProcess 默认值 = false / model.serviceUrl 默认值 = http://172.17.0.1:18080
PASS  渲染后 path 里有 scope/taskTypes/includeSubProcess/serviceUrl（共 17 个 element，widget 全部可渲染）
RESULT: ALL_PASS（全部断言通过）
```

对照实验（同样的断言跑 SQL 表单 `Rr`）**17 项 FAILED**，命中 `[type,datasource,sqlType,displayRows,
connParams,sql,preStatements,postStatements]` + `type='editor' field="sql"` —— 说明断言不是空转，
也复现了用户看到的那张「SQL 表单」。

**前端补丁维护要点（换海豚版本时必看）**

* **文件名带 content-hash**：`detail.<hash>.js` 随海豚版本变；`prep_ds_demo.sh` 按
  `ui-static/assets/` 下的文件名去容器里 `md5sum` 比对，找不到就 `⚠️ 容器内无 assets/xxx.js` 报出来，
  这时要用 `docker exec ds-standalone ls ui/assets | grep '^detail'` 找到新名字再改名归档。
* **压缩变量名会变**：`Rr/Er/k/O/L/N/S/E/w/R/A/P/LDF` 这些名字是 esbuild 压缩产物，跨版本不保证稳定。
  重新适配的锚点是 `const oa={…}`（映射表）与 `return{json:[k(t),...O({…}),L(),N(),S(),E(e),w(r,…),
  ...R(r,e),...A(r),…,P()],model:r}`（SUB_PROCESS 那一段），按锚点重新定位即可。
* **强缓存**：改名后浏览器仍可能拿旧文件（文件名 hash 是强缓存键）→ 部署后 **Ctrl+Shift+R**。
* **`.gz` 副本**：容器 `ui/assets/` 里每个 bundle 都有同名 `.gz`。`prep_ds_demo.sh` 现在会在应用补丁后
  `rm -f` 再 `gzip -9 -n -k` 重建它，并且在「.js 已是最新」的分支里也会 `zcat | md5sum` 核一遍
  （不一致就重建）—— 防的是「`.js` 换了、`.gz` 还是旧内容」这种只改一半的情况。
  > 实测补充：本环境的 UI 由 api-server 动态 gzip 下发。我把容器里的 `detail…js.gz` 故意换成旧补丁的
  > 压缩内容后请求 `Accept-Encoding: gzip`，服务端返回的仍是**新内容**（`Content-Length` 与 `original
  > size` 都对得上当前 `.js`）—— 说明当前部署链路里 `.gz` 并不是命中文件。保留同步只是**前置 nginx
  > `gzip_static` 的保险**，避免换部署方式后踩坑。
* **`LINEAGE`（单脚本）的表单保持 `Rr`**：它有真实的 SQL 输入需求，不要跟着改。

### 7.9 服务端：`POST /analyze-workflow`

实现在 `lineage/workflow.py`（`api_server.py` 只做路由与异常包装）。请求 / 响应：

```bash
curl -s -X POST http://localhost:18080/analyze-workflow -H 'Content-Type: application/json' -d '{
  "project_code": 184812330295872, "process_define_code": 184812330396224, "scope": "current",
  "task_types": ["SQL","SHELL","PYTHON"], "include_sub_process": false,
  "with_knowledge": true, "with_report": true }'
```

```json
{ "success": true,
  "workflow": {"name":"wf_dwd_清洗","code":184812330396224,"task_count":4,"parsed_task_count":4,
               "statement_count":4,"chain":["src.erp_生产工单明细","ods.ods_卷烟产量流水","cdw.dwd_卷烟产量明细"],
               "chain_in_workflow":["ods.ods_卷烟产量流水","cdw.dwd_卷烟产量明细"],
               "chain_external":{"upstream":["src.erp_生产工单明细"],"downstream":["cdw.dws_产量汇总", …]}},
  "tasks": [{"name":"t_dwd_产量明细","type":"SQL","script_len":1085,"statement_count":1,
             "input_tables":["dim.dim_brand","dim.dim_plant","ods.ods_卷烟产量流水"],
             "output_tables":["cdw.dwd_卷烟产量明细"],"metric_count":2,"errors":[]}, …],
  "merged": {"table_lineage":[…9 条…],"nodes":[10 张表],"edges":[9 条],"column_lineage_count":43},
  "chain": ["src.erp_生产工单明细","ods.ods_卷烟产量流水","cdw.dwd_卷烟产量明细"],
  "knowledge": {"kb_available":true,"metric_count":5,"terms":30,"rules":5},
  "quality": {"dangling_outputs":[4 条(全部标注「下游在别的工作流」)],"orphan_inputs":[],
              "cycles":[],"missing_knowledge":[]},
  "cost_ms": 27, "report_id": "rpt_20260920_213348_0e59336e",
  "url": "http://localhost:18080/report/rpt_20260920_213348_0e59336e",
  "internal_url": "http://172.17.0.1:18080/report/rpt_20260920_213348_0e59336e" }
```

报告页在 `meta.workflow` 存在时切到**工作流模式**：顶部「工作流概览」= 任务清单表格 +
全链路面包屑 + 链路质量体检徽标；字段级血缘表多一列「来源任务」；第 ④ 段换成**全链路 DAG SVG**
（按最长路径分层，左=贴源、右=应用层）。单任务报告**一个字节都没变**（`test_render_report_single_task_unchanged` 守着）。

### 7.10 本章的限制（如实说明）

* **前端交互渲染没有用真浏览器验证**：本环境的浏览器工具调用会超时（Phase 5 报告 HTML 时
  也是同样情况），所以 UI 部分给的是「可复现的结构性证据」：
  4 个 bundle 在服务端 **HTTP 200**（`.gz` 副本同步、内容 md5 与仓库归档一致）；
  与**镜像原版**逐一 diff，`detail.*.js` 只有 **2 处**差异（插入 `function LDF(…)` + `LINEAGE_DAG:`
  的值 `Rr`→`LDF`），`detail-modal.*.js` 只有 **2 处插入**（类型表 + 保存白名单）；
  **表单函数与保存函数都是真跑过的**（`verify_ldf_form.py`：抽 `LDF` 原样执行，再把 `json` 喂给真实
  渲染管线 `We()`，17 个 element 全部渲染成功、无 SQL/数据源字段；同一套断言跑 SQL 表单 `Rr` 会 17 项
  FAIL，证明断言不空转。`verify_save_params.py`：`LDF` 的 model 喂给真实保存函数 `Ne()`，补丁后
  `taskParams` 带全 4 个字段、改值能生效）；
  `dynamic-task-type-config.yaml` 与 `lineage-dag.json` 都已进容器且可 200 取到。
  **请按 Ctrl+Shift+R 硬刷新后在浏览器里确认一次拖拽与弹窗渲染**。
* **`LINEAGE`（单脚本）从 UI 保存会丢类型参数**（历史遗留，本次未改）：`detail-modal.*.js` 的保存白名单
  `Ne()` 里没有 `LINEAGE` 分支，`mode/sql/dialect/serviceUrl` 全部丢失；而
  `LineageParameters.checkParameters()` 要求 `sql` 非空 ⇒ 该类型从 UI 保存后跑不起来。
  本仓库的 LINEAGE 演示与验证全部走 **OpenAPI 建流**（参数直接写在 `taskParams` 里），所以不受影响。
  修法与 7.8.1 给 LINEAGE_DAG 补的那一行完全同构（`e.taskType==="LINEAGE"&&(r.mode=e.mode,r.sql=e.sql,…)`），
  但该类型的表单（`Rr`）本身没有 `serviceUrl/dialect` 输入框，要一起补齐才有意义 —— 属于下一步工作。
* 演示环境没有真实 HIVE 数据源 ⇒ SQL 任务 FAILURE ⇒ 尾部串联（依赖上游成功）的节点会被海豚跳过；
  这不影响插件能力（读定义，不读结果），但「尾部串联 + 真跑成功」需要可用的数据源才能复现。
* 环路检测在表级依赖图上做（DFS 找环，最多报 5 个）；字段级不成环、工作流原生 DAG 不成环，
  所以正常情况下 `cycles` 恒为空 —— 它防的是「脚本里读了自己写的表」这类**数据层环**。
* `scope=project` 会把项目下所有工作流一起解析（项目很大时耗时会随之上升，没有做并发）。
* 单脚本解析的固有局限（`SELECT *` 展开、同名歧义列等）与第 6 节一致，工作流级只是把它们合并起来展示。
