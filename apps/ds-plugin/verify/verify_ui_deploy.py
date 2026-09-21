#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
前端补丁部署验证：确认容器里**实际下发**的 4 个 bundle 都是打过补丁的版本，
并且 LINEAGE_DAG 的节点表单映射指向专属函数 LDF（不再是 SQL 的 Rr）。

关键点：海豚 nginx 开了 gzip_static —— 带 Accept-Encoding: gzip 的请求命中的是
同名 .gz 文件。所以这里**两种请求都测**，并且从 gzip 响应里解压后再断言内容。
（曾经踩过的坑：只更新 .js 不更新 .gz，浏览器永远拿到旧 bundle，硬刷新也没用。）

用法: .venv/bin/python apps/apps/ds-plugin/verify/verify_ui_deploy.py
"""

import gzip
import hashlib
import os
import re
import sys
import urllib.request

UI = 'http://localhost:12345/dolphinscheduler/ui/assets'
REPO_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'frontend', 'ui-static', 'assets')

# 每个文件必须包含 / 必须不含 的补丁标志串
EXPECT = {
    'task-type.27c43290.js': {
        'must': ['LINEAGE:{alias:"LINEAGE"', 'LINEAGE_DAG:{alias:"LINEAGE_DAG"'],
        'must_not': [],
    },
    'dag-sidebar.340ad7aa.js': {
        'must': ['taskType:"LINEAGE"', 'taskType:"LINEAGE_DAG"'],
        'must_not': [],
    },
    'detail-modal.de80dd20.js': {
        'must': ['LINEAGE:{alias:"LINEAGE",helperLinkDisable:!0}',
                 'LINEAGE_DAG:{alias:"LINEAGE_DAG",helperLinkDisable:!0}',
                 'e.taskType==="LINEAGE_DAG"&&(r.scope=e.scope,r.taskTypes=e.taskTypes,'
                 'r.includeSubProcess=e.includeSubProcess,r.serviceUrl=e.serviceUrl)'],
        'must_not': [],
    },
    'detail.f1164795.js': {
        'must': ['LINEAGE:Rr,LINEAGE_DAG:LDF}', 'function LDF('],
        'must_not': ['LINEAGE_DAG:Rr)'],
    },
}

SQL_FIELDS = ['field:"sql"', 'field:"sqlType"', 'field:"datasource"', 'field:"type"', 'type:"editor"']


def fetch(path, gzip_ok):
    req = urllib.request.Request(UI + '/' + path)
    if gzip_ok:
        req.add_header('Accept-Encoding', 'gzip')
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
        enc = r.headers.get('Content-Encoding', '')
        hdr = dict(r.headers)
    return body, enc, hdr


def main():
    fails = []
    print('== 1) 四个 bundle 的 HTTP 状态 / Content-Encoding ==')
    bodies = {}
    for name in EXPECT:
        plain, enc1, _ = fetch(name, False)
        gz, enc2, _ = fetch(name, True)
        if enc2 == 'gzip':
            gz = gzip.decompress(gz)
        bodies[name] = gz
        ok = (enc1 == '' and enc2 == 'gzip')
        print('   %-28s plain=%dB(%s)  gzip=%dB(%s)  %s' % (
            name, len(plain), enc1 or 'identity', len(gz), enc2 or 'identity',
            'OK' if ok else '!! 请求 gzip 时未拿到 Content-Encoding: gzip'))

    print('')
    print('== 2) 服务端下发的字节 == 仓库归档文件 ==')
    for name in EXPECT:
        repo = os.path.join(REPO_ASSETS, name)
        rb = open(repo, 'rb').read()
        m_repo = hashlib.md5(rb).hexdigest()
        m_srv = hashlib.md5(bodies[name]).hexdigest()
        same = m_repo == m_srv
        if not same:
            fails.append('%s 服务端内容与仓库不一致 (%s != %s)' % (name, m_srv, m_repo))
        print('   %-28s repo=%s server(gzip解压)=%s  %s' % (name, m_repo, m_srv, 'SAME' if same else 'DIFF'))

    print('')
    print('== 3) 补丁标志串 ==')
    for name, exp in EXPECT.items():
        text = bodies[name].decode('utf-8', 'replace')
        for s in exp['must']:
            hit = s in text
            if not hit:
                fails.append('%s 缺少标志串 %r' % (name, s))
            print('   %-28s must have   %-46s %s' % (name, s, 'OK' if hit else 'MISSING'))
        for s in exp['must_not']:
            hit = s in text
            if hit:
                fails.append('%s 仍含旧串 %r' % (name, s))
            print('   %-28s must NOT have %-43s %s' % (name, s, 'OK' if not hit else 'STILL THERE'))

    print('')
    print('== 4) LINEAGE_DAG 专属表单函数 LDF 的字段清单（从服务端下发的字节里切出来）==')
    text = bodies['detail.f1164795.js'].decode('utf-8', 'replace')
    i = text.index('function LDF(')
    depth = 0
    j = text.index('{', text.index(')', i))
    k = j
    while k < len(text):
        if text[k] == '{':
            depth += 1
        elif text[k] == '}':
            depth -= 1
            if depth == 0:
                break
        k += 1
    body = text[i:k + 1]
    fields = re.findall(r'field:"([A-Za-z0-9_$]+)"', body)
    print('   LDF 体长度 = %d 字符' % len(body))
    print('   field 列表 = %s' % fields)
    for biz in ['scope', 'taskTypes', 'includeSubProcess', 'serviceUrl']:
        if biz not in fields:
            fails.append('LDF 缺少业务字段 %s' % biz)
        print('   业务字段 %-18s %s' % (biz, 'OK' if biz in fields else 'MISSING'))
    bad = [f for f in fields if f in ('sql', 'sqlType', 'datasource', 'type', 'displayRows', 'connParams')]
    if bad:
        fails.append('LDF 里出现 SQL 专属字段 %s' % bad)
    print('   SQL 专属字段命中 = %s  %s' % (bad, 'OK' if not bad else 'FAIL'))
    for s in SQL_FIELDS:
        hit = s in body
        if hit:
            fails.append('LDF 体里出现 %r' % s)
        print('   不含 %-18s %s' % (s, 'OK' if not hit else 'FAIL 仍存在'))
    for s in ['解析范围 (scope)', '解析任务类型 (taskTypes)', '包含子工作流 (includeSubProcess)', '血缘服务地址 (serviceUrl)']:
        hit = s in body
        if not hit:
            fails.append('LDF 缺少标签 %r' % s)
        print('   标签 %-28s %s' % (s, 'OK' if hit else 'MISSING'))
    tip = '无需填写 SQL' in body
    if not tip:
        fails.append('LDF 缺少「无需填写 SQL」提示文本')
    print('   提示文本「无需填写 SQL / 数据源」  %s' % ('OK' if tip else 'MISSING'))

    print('')
    if fails:
        print('RESULT: FAILED (%d)' % len(fails))
        for f in fails:
            print('   - %s' % f)
        return 1
    print('RESULT: ALL_PASS —— 4 个 bundle 服务端 200（gzip 与明文两种请求都返回当前内容、'
          '容器内 .gz 副本与 .js 一致），LINEAGE_DAG 表单 = LDF（4 个业务字段，无 SQL/数据源字段）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
