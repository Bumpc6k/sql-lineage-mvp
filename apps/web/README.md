# apps/web —— 独立前端模块（血缘工作台）

平台里的**单元①**：只通过 HTTP 调 `apps/lineage-api`，不 import 任何后端代码。
后端挂了/换地址，改顶部「服务地址」即可，前端本身不需要改一行。

## 现在有什么（最小闭环两屏）

| 屏 | 干什么 | 用到的接口 |
| --- | --- | --- |
| 血缘查询 | 粘一段 SQL → 解析出**表级血缘 / 字段级血缘 / 命中的业务口径**，一键跳报告；也能按表名查**上游溯源 / 下游影响面**（分层展示 + 链路） | `POST /analyze`、`POST /upstream`、`POST /impact`、`GET /health` |
| 报告浏览 | 列出服务端已生成的 HTML 报告（时间/大小），**内嵌预览**或新窗口打开 | `GET /reports`、`GET /report/<id>` |

## 怎么起（两种方式，任选）

```bash
# A. 独立部署（体现「单元①独立」）：静态服务 :5173
bash ops/start-web.sh                  # 默认 5173，可传端口：bash ops/start-web.sh 5199
#   → 浏览器开 http://localhost:5173

# B. 蹭后端托管（演示最省事）：不用起任何前端服务
bash ops/start-lineage-api.sh          # 后端 :18080
#   → 浏览器开 http://localhost:18080/app/
```

后端没起时页面也能打开，只是顶栏显示「连不上」并提示改地址。

## 目录

```
apps/web/
├── index.html                    两屏的模板（Vue 3 的 in-DOM 模板，无编译）
├── app.js                        应用逻辑（fetch 调后端 + 状态/方法）
├── style.css                     深色主题（与服务端报告页风格一致）
├── vendor/vue.global.prod.js     Vue 3.5.43 本地内置 —— 不依赖 CDN，离线可跑
└── README.md
```

## 技术取舍（为什么现在没有 Vite / TS）

* 本机**没有 Node/npm**（WSL 与 Windows 侧都没有），装 Node 才能跑 Vite。
* 最小闭环的目标是「**能演示**」：所以先用**零构建**路线 —— Vue 3 全局构建 + 原生 ES 模块，
  双击/静态服务就能跑，**不需要任何构建步骤**，部署就是把目录拷过去。
* 升级路径已留好：`app.js` 是标准 ES 模块、模板与逻辑分离、接口调用集中在 `call()` 一个方法里。
  装好 Node 后可按 `contracts/openapi.yaml` 生成 TS 类型 + 拆 `.vue` 单文件组件，
  **目录结构（`apps/web`）与契约都不用动**。

## 契约

* 请求/响应字段以 `contracts/openapi.yaml` 为准（该文件由 `tools/gen_openapi.py` 从后端真实路由生成）。
* 后端为跨源调用开了 CORS（`Access-Control-Allow-Origin`，可用环境变量 `LINEAGE_CORS_ORIGIN` 收紧）。
* 只允许**追加**字段/端点；破坏性变更必须升 `/v2`。
