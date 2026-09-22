#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 api_server 的**真实路由表**生成 contracts/openapi.yaml。

契约落文件是「独立部署」的前提：前端（apps/web）、海豚插件（apps/ds-plugin）、
以及未来新平台的 integrations/lineage-client 都只认这个文件。

本模块有两个入口，且共用同一份生成逻辑（这是防漂移的关键）：
  ① CLI：``python3 tools/gen_openapi.py``  → 把契约写回 contracts/openapi.yaml
  ② 测试：``tests/test_contract.py`` 调 ``render_spec()`` 与文件逐字节比对
     —— 改了路由却忘记重新生成契约，测试立刻红。

路由来源（唯一事实源）：
  apps/lineage-api/lineage/serve/api_server.py 的 ROUTES / GET_ROUTES / DYNAMIC_GET_ROUTES
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "contracts" / "openapi.yaml"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_APP = ROOT / "apps" / "lineage-api"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from lineage.serve.api_server import DYNAMIC_GET_ROUTES, GET_ROUTES, ROUTES  # noqa: E402

#: 端点说明：路径 →（summary, description）
DESC = {
    '/health': ('服务健康检查', '存活探针：给容器/运维用，不校验参数'),
    '/parse': ('SQL 血缘解析（单个脚本）', '输入 SQL + 方言，输出表级/字段级血缘与语句数'),
    '/analyze': ('单脚本血缘分析 + 业务口径', '带知识库口径命中的分析，是 LINEAGE 任务调用的主端点'),
    '/analyze-workflow': ('工作流级血缘分析', '按工作流身份拉取全部任务脚本再合并解析，是 LINEAGE_DAG 任务调用的主端点'),
    '/impact': ('下游影响面', '给一张表，返回受影响的下游表/字段'),
    '/upstream': ('上游溯源', '给一张表，返回它的上游链路'),
    '/report': ('生成单文件 HTML 报告', 'POST 分析结果 -> 落盘 HTML -> 返回可点击 URL'),
    '/report/<report_id>': ('报告 HTML（按 ID 取回）', '凭证（Evidence）的最终落地页：前端 iframe 内嵌、插件日志给链接、可直接发同事'),
    '/reports': ('报告列表', '列出已生成的报告'),
    '/kb/summary': ('知识库概览', '口径/字段/规则的数量统计'),
    '/kb/search': ('知识库检索', '按关键词检索口径与术语'),
    '/kb/ask': ('口径问答', '自然语言问口径；LLM 可插拔（未配置时走确定性回答）'),
    '/kb/metric': ('口径详情', '按指标码取口径定义与来源'),
    '/generate/sql': ('生成加工 SQL', '给定源表/目标表/指标/分组，套模板生成 SQL（确定性）'),
    '/generate/pipeline': ('生成分层链路', '给定需求描述，产出多阶段 pipeline（可接 LLM 做模糊需求）'),
    '/generate/apply': ('把链路落到海豚', '建/改海豚工作流（apply）—— 写操作，新平台 P4 才用'),
    '/generate/validate': ('反向校验', '把生成的 SQL 再解析一遍，与预期血缘比对'),
}


def implemented_routes() -> tuple[set, set]:
    """返回（POST 路径集合, GET 路径集合），含动态路径，路径写法用 ``<param>``。"""
    post = set(ROUTES.keys())
    get = set(GET_ROUTES) | set(DYNAMIC_GET_ROUTES)
    return post, get


def spec_path(path: str) -> str:
    """``/report/<report_id>`` → ``/report/{report_id}``（OpenAPI 参数写法）。"""
    return path.replace("<", "{").replace(">", "}")


def render_spec() -> str:
    """按真实路由渲染契约文本（不落盘）。"""
    post_paths, get_paths = implemented_routes()
    paths = sorted(post_paths | get_paths)
    lines = [
        '# -*- coding: utf-8 -*-',
        '# 血缘服务对外 HTTP 契约 —— 由 tools/gen_openapi.py 从真实路由表生成，请勿手改',
        '# 生成命令: python3 tools/gen_openapi.py   （路由表来源: apps/lineage-api/lineage/serve/api_server.py）',
        '# 漂移守卫: tests/test_contract.py（契约与路由不一致即测试失败）',
        'openapi: "3.0.3"',
        'info:',
        '  title: LINEAGE 服务（血缘分析 / 业务口径 / 链路生成）',
        '  version: "1.0.0"',
        '  description: |',
        '    这是**独立部署单元** apps/lineage-api 的对外接口，也是 apps/web 前端的唯一耦合面。',
        '    契约规则：只允许**追加**字段/端点；破坏性变更必须升 /v2 并保留旧版一段时间。',
        '    调用方：① apps/web 前端  ② apps/ds-plugin（海豚插件，走容器内 172.17.0.1:18080）',
        'servers:',
        '  - url: http://localhost:18080',
        '    description: 宿主机直连（浏览器 / 前端 dev）',
        '  - url: http://172.17.0.1:18080',
        '    description: 容器内访问宿主（海豚任务用）',
        'paths:',
    ]
    for p in paths:
        desc, note = DESC.get(p, ('', ''))
        methods = []
        if p in post_paths:
            methods.append('post')
        if p in get_paths:
            methods.append('get')
        lines.append('  %s:' % spec_path(p))
        for m in sorted(methods, reverse=True):  # get 在 post 前，与既有文件顺序一致
            lines.append('    %s:' % m)
            lines.append('      summary: %s' % (desc or p))
            lines.append('      description: %s' % (note or '（实现见 apps/lineage-api/lineage/serve/api_server.py）'))
            if m == 'get' and '<' in p:
                param = p.split('<', 1)[1].split('>', 1)[0]
                lines.append('      parameters:')
                lines.append('        - name: %s' % param)
                lines.append('          in: path')
                lines.append('          required: true')
                lines.append('          schema:')
                lines.append('            type: string')
            lines.append('      responses:')
            lines.append("        '200':")
            lines.append('          description: 统一返回 {"success": bool, ...}；失败时带 error 字段')
            if m == 'post':
                lines.append('      requestBody:')
                lines.append('        required: true')
                lines.append('        content:')
                lines.append('          application/json:')
                lines.append('            schema:')
                lines.append('              type: object')
                lines.append('              description: 见 apps/ds-plugin/verify/verify_api.py 的真实请求体样例')
    return '\n'.join(lines) + '\n'


def main() -> None:
    post_paths, get_paths = implemented_routes()
    paths = sorted(post_paths | get_paths)
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(render_spec(), encoding='utf-8')
    print('已生成 %s：%d 个端点' % (SPEC_PATH.relative_to(ROOT), len(paths)))
    for p in paths:
        ms = sorted({m.upper() for m in ('post',) if p in post_paths} | {m.upper() for m in ('get',) if p in get_paths})
        print('   %-24s %s' % (p, '/'.join(ms)))


if __name__ == '__main__':
    os.chdir(ROOT)
    main()
