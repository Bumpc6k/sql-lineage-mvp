#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 api_server 的真实路由生成 contracts/openapi.yaml（契约落文件，独立部署的前提）。"""
import os
import sys

ROOT = '/root/projects/sql-lineage-mvp'
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, 'apps/lineage-api'))

from lineage.serve.api_server import GET_ROUTES, ROUTES  # noqa: E402

DESC = {
    '/health': ('服务健康检查', '存活探针：给容器/运维用，不校验参数'),
    '/parse': ('SQL 血缘解析（单个脚本）', '输入 SQL + 方言，输出表级/字段级血缘与语句数'),
    '/analyze': ('单脚本血缘分析 + 业务口径', '带知识库口径命中的分析，是 LINEAGE 任务调用的主端点'),
    '/analyze-workflow': ('工作流级血缘分析', '按工作流身份拉取全部任务脚本再合并解析，是 LINEAGE_DAG 任务调用的主端点'),
    '/impact': ('下游影响面', '给一张表，返回受影响的下游表/字段'),
    '/upstream': ('上游溯源', '给一张表，返回它的上游链路'),
    '/report': ('生成单文件 HTML 报告', 'POST 分析结果 -> 落盘 HTML -> 返回可点击 URL'),
    '/reports': ('报告列表', '列出已生成的报告'),
    '/kb/summary': ('知识库概览', '口径/字段/规则的数量统计'),
    '/kb/search': ('知识库检索', '按关键词检索口径与术语'),
    '/kb/ask': ('口径问答', '自然语言问口径；LLM 可插拔（未配置时走确定性回答）'),
    '/kb/metric': ('口径详情', '按指标码取口径定义与来源'),
    '/generate/sql': ('生成加工 SQL', '给定源表/目标表/指标/分组，套模板生成 SQL（确定性）'),
    '/generate/pipeline': ('生成分层链路', '给定需求描述，产出多阶段 pipeline（可接 LLM 做模糊需求）'),
    '/generate/apply': ('把链路落到海豚', '建/改海陵工作流（apply）'),
    '/generate/validate': ('反向校验', '把生成的 SQL 再解析一遍，与预期血缘比对'),
    '/': ('服务首页', '端点清单'),
    '/report/': ('报告 HTML', 'GET 具体报告页面'),
}


def main():
    os.makedirs('contracts', exist_ok=True)
    post_paths = set(ROUTES.keys() if hasattr(ROUTES, 'keys') else ROUTES)
    get_paths = set(GET_ROUTES.keys() if hasattr(GET_ROUTES, 'keys') else GET_ROUTES)
    paths = sorted(post_paths | get_paths)
    lines = [
        '# -*- coding: utf-8 -*-',
        '# 血缘服务对外 HTTP 契约（由 apps/ds-plugin/... 不需要，本文件由 tools/gen_openapi.py 从真实路由生成）',
        '# 生成命令: python3 tools/gen_openapi.py   （路由表来源: apps/lineage-api/lineage/serve/api_server.py）',
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
        lines.append('  %s:' % p)
        for m in methods:
            lines.append('    %s:' % m)
            lines.append('      summary: %s' % (desc or p))
            lines.append('      description: %s' % (note or '（实现见 apps/lineage-api/lineage/serve/api_server.py）'))
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
    open('contracts/openapi.yaml', 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print('已生成 contracts/openapi.yaml：%d 个端点' % len(paths))
    for p in paths:
        print('   %-24s %s' % (p, '/'.join(sorted({m.upper() for m in (['post'] if p in post_paths else []) + (['get'] if p in get_paths else [])}))))


if __name__ == '__main__':
    main()
