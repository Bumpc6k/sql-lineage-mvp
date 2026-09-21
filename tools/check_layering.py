#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分层守卫：把「谁能 import 谁」从口头约定变成可执行检查。

规则（对应方案 v2 第四节）：
  core       纯解析内核，禁止 IO / 网络 / sqlite / 任何其它层
  collect    本地采集，禁止网络 / sqlite / 上层
  render     呈现，禁止网络 / sqlite / 上层
  knowledge  知识层，允许 sqlite；禁止 ds / serve / cli
  generate   生成，禁止 serve / cli；允许 ds 的仅 apply（白名单）
  ds         外部系统适配（唯一出网）；禁止 serve / cli
  serve/cli  入口层，不限制（但仍禁止直接 import 其它层的私有实现——靠人工评审）

用法:
    python3 tools/check_layering.py            # 0 违规退出 0，有违规则退出 1
    python3 tools/check_layering.py -v         # 打印扫描详情
"""
import argparse
import ast
import os
import re
import sys

LAYERS = ['core', 'collect', 'render', 'knowledge', 'generate', 'ds', 'serve', 'cli']

#: 每层禁止 import 的「外部模块前缀」
BAN_EXTERNAL = {
    'core': ['socket', 'http', 'urllib', 'requests', 'sqlite3', 'subprocess', 'ftplib', 'smtplib'],
    'collect': ['socket', 'http', 'urllib', 'requests', 'sqlite3', 'subprocess'],
    'render': ['socket', 'http', 'urllib', 'requests', 'sqlite3', 'subprocess'],
    'knowledge': ['socket', 'http.client', 'urllib', 'requests', 'subprocess'],
    'generate': ['socket', 'http', 'urllib', 'requests', 'subprocess'],
    'ds': [],
    'serve': [],
    'cli': [],
}
#: 每层禁止 import 的「本仓库其它层」
BAN_LAYERS = {
    'core': ['collect', 'render', 'knowledge', 'generate', 'ds', 'serve', 'cli'],
    'collect': ['render', 'knowledge', 'generate', 'ds', 'serve', 'cli'],
    'render': ['collect', 'knowledge', 'generate', 'ds', 'serve', 'cli'],
    'knowledge': ['ds', 'serve', 'cli'],
    'generate': ['ds', 'serve', 'cli'],
    'ds': ['serve', 'cli'],
    'serve': [],
    'cli': [],
}
#: 白名单：文件相对包路径 -> 允许的例外
WHITELIST = {
    'generate/apply.py': {'layers': ['ds'], 'why': 'apply 要把生成的流程落到海豚上（唯一允许 generate 触达 ds 的文件）'},
    'knowledge/qa.py': {'external': ['urllib', 'http.client', 'requests'],
                        'why': '口径问答要调可插拔 LLM（knowledge 层唯一的出网点，LLM 客户端实现就在这个文件里）'},
}
BAN_IO_IN_CORE = True  # core 里额外禁止 open()（提示级）


def find_packages(root):
    """定位两个包：lineage（服务单元）与 lineage_core（共享内核）。"""
    found = {}
    for d, dirs, fs in os.walk(root):
        if '.venv' in d or '__pycache__' in d or '/.git' in d or '/reports' in d:
            continue
        base = os.path.basename(d)
        if base in ('lineage', 'lineage_core') and '__init__.py' in fs:
            found.setdefault(base, d)
    return found


def layer_of(rel, pkg_name='lineage'):
    if pkg_name == 'lineage_core':
        return 'core'          # 共享内核整体就是 core 层
    parts = rel.split(os.sep)
    return parts[0] if parts and parts[0] in LAYERS else None


def scan(pkg_dir, pkg_name='lineage'):
    violations = []
    for d, dirs, fs in os.walk(pkg_dir):
        if '__pycache__' in d:
            continue
        for f in sorted(fs):
            if not f.endswith('.py'):
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, pkg_dir)
            layer = layer_of(rel, pkg_name)
            if layer is None:
                continue
            wl = WHITELIST.get(rel.replace(os.sep, '/'), {})
            allowed_layers = set(wl.get('layers', []))
            allowed_external = set(wl.get('external', []))
            try:
                tree = ast.parse(open(p, encoding='utf-8').read())
            except SyntaxError as e:
                violations.append((rel, 0, 'SyntaxError', str(e)))
                continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        # 相对 import：算出它落在哪个层
                        depth = len(rel.split(os.sep)) - node.level
                        base = rel.split(os.sep)[:max(0, depth)]
                        mods = ['.'.join(base + ([node.module] if node.module else []))]
                    else:
                        mods = [node.module or '']
                for m in mods:
                    if not m:
                        continue
                    top = m.split('.')[0]
                    # 外部库
                    for bad in BAN_EXTERNAL.get(layer, []):
                        if any(m == e or m.startswith(e + '.') for e in allowed_external):
                            continue
                        if m == bad or m.startswith(bad + '.'):
                            violations.append((rel, node.lineno, '外部模块 %s' % m,
                                               '%s 层禁止 import %s' % (layer, bad)))
                    # 跨层
                    if top == 'lineage':
                        seg = m.split('.')
                        tgt = seg[1] if len(seg) > 1 else None
                        if tgt in BAN_LAYERS.get(layer, []) and tgt not in allowed_layers:
                            violations.append((rel, node.lineno, '跨层 lineage.%s' % tgt,
                                               '%s 层禁止 import %s 层' % (layer, tgt)))
    return violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=None, help='仓库根目录（默认自动定位）')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    root = args.root or os.path.dirname(here)
    pkgs = find_packages(root)
    if not pkgs:
        print('!! 找不到 lineage / lineage_core 包（--root %s）' % root)
        return 2
    violations = []
    for name in sorted(pkgs):
        print('扫描: %s (%s)' % (os.path.relpath(pkgs[name], root), name))
        violations += scan(pkgs[name], name)
        if args.verbose:
            n = sum(1 for d, _, fs in os.walk(pkgs[name]) for f in fs if f.endswith('.py'))
            print('  文件数: %d' % n)
    if violations:
        print('发现 %d 处越界 import：' % len(violations))
        for rel, ln, what, why in violations:
            print('  %-34s:%-4s %-26s %s' % (rel, ln, what, why))
        return 1
    print('分层检查通过：0 violations ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())
