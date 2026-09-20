# DolphinScheduler 自定义任务类型插件：LINEAGE（血缘分析）

给 Apache DolphinScheduler **3.2.2**（standalone 单体容器 `ds-standalone`）加一个自定义任务类型
`LINEAGE`：在工作流里像用 SHELL/SQL 任务一样用它，运行时通过 HTTP 调用宿主上的血缘服务
（`http://172.17.0.1:18080`），把血缘报告写进**任务实例日志**，同时把关键结果写成**出参**
（`varPool`）供下游任务引用。

- 插件类型名：`LINEAGE`
- 插件实现：`org.apache.dolphinscheduler.plugin.task.lineage.*`
- 依赖：只依赖海豚自身的 `dolphinscheduler-task-api` / `dolphinscheduler-spi` /
  `dolphinscheduler-common`（`JSONUtils`）+ `jackson` + `slf4j`，HTTP 用 JDK 自带
  `HttpURLConnection`，**不需要额外第三方库**
- 注册方式：手写 `META-INF/services/org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory`
  （不用 `@AutoService`，避免引入 auto-service 依赖）

---

## 1. 目录结构

```
/root/projects/ds-lineage-plugin/
├── build.sh                       # 编译 + 打包 + 拷进容器 + 重启海豚（一键）
├── deploy_ui.sh                   # 把动态任务类型配置/表单 JSON/图标拷进容器并重启（UI 可见用）
├── verify.sh                      # 部署与功能验证（真实请求，输出即证据）
├── verify_api.py                  # verify.sh 第 6 步调用的端到端脚本（被 verify.sh 调用）
├── verify_knowledge.py            # 「产量口径」SQL 端到端：断言 ①~⑤ + 表格/折叠/📊 报告 URL，并回读报告
├── verify_impact_mode.py          # mode=impact 回归（第 ⑤ 段与报告行都不出现）
├── README.md                      # 本文件
├── dolphinscheduler-task-lineage-3.2.2.jar   # 构建产物
├── libs/                          # 从容器导出的编译依赖 jar（build.sh 自动导出）
├── build/                         # 编译中间产物 + verify_output.txt（验证原始输出）
├── conf/dynamic-task-type-config.yaml        # 改过的海豚配置（源文件，容器内是副本）
├── src/main/java/org/apache/dolphinscheduler/plugin/task/lineage/
│   ├── LineageTaskChannelFactory.java   # SPI 入口：getName()="LINEAGE"，getParams() 定义 UI 表单
│   ├── LineageTaskChannel.java          # TaskChannel：createTask / parseParameters
│   ├── LineageParameters.java           # 任务参数（继承 AbstractParameters，带 checkParameters）
│   ├── LineageTask.java                 # 核心：拼 JSON -> HTTP POST -> 解析 -> 打日志 -> 写出参
│   └── LineageServiceClient.java        # JDK HttpURLConnection 封装的极小 HTTP 客户端
├── src/main/resources/META-INF/services/
│   └── org.apache.dolphinscheduler.plugin.task.api.TaskChannelFactory
└── ui-static/lineage/             # UI 用：动态任务类型表单 JSON + 图标
    ├── lineage.json
    ├── lineage-icon.svg
    └── lineage-hover.svg
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
     --data-binary @/tmp/lineage_report_body.json          # 见 scripts/report_evidence.sh
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
> 结构/样式以上面的片段与 `bash scripts/report_evidence.sh` 的自检清单为准（10 项全 ✅）。
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
> `.venv/bin/python ds-plugin/verify_knowledge.py`。该脚本会断言这张表里的每一个特征，
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

`verify_knowledge.py --degraded` 配合 `scripts/p5_legacy_proxy.py`（把 `/analyze` 变成 404、
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
这两条路与改造前完全一致（已实测，见 `scripts/p5_verify_all.sh` 第 4 步）。

## 6. 已知坑 / 限制（踩过的）

1. **海豚 standalone 用内存 H2**（`jdbc:h2:mem:dolphinscheduler`），
   `docker restart ds-standalone` 之后**所有项目、工作流、调度、实例数据都会清空**。
   原有的「烟草数仓演示」项目 + 4 个演示工作流就是这么没的，需要重跑
   `/root/projects/sql-lineage-mvp` 里的
   `.venv/bin/python scripts/ds_setup_demo.py --no-export` 重新生成
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
