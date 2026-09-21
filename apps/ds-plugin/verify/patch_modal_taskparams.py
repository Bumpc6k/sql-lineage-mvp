#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 detail-modal.<hash>.js 补一处：**保存链路**里 LINEAGE_DAG 的 taskParams 白名单。

背景（实测）
------------
节点设置弹窗点「保存」时，payload 由 detail-modal.<hash>.js 里的 `Ne(e)` 拼装：

    const r={};                                   // r = 类型专属 taskParams
    if((e.taskType==="SUB_PROCESS"||…))…          // 25 个任务的 if 链，逐个白名单拷贝
    e.taskType==="DYNAMIC"&&(r.processDefinitionCode=e.processDefinitionCode,…);
    …
    taskParams:{localParams,initScript,rawScript,resourceList:[],…r}

这条 if 链里**没有 LINEAGE / LINEAGE_DAG 分支**，于是这两个任务类型保存时 `r` 恒为 `{}`，
类型参数被整包丢掉：
  * LINEAGE      → 丢 mode/sql/dialect/serviceUrl，Java 侧 checkParameters() 里 sql 非空这一条直接不过；
  * LINEAGE_DAG  → 丢 scope/taskTypes/includeSubProcess/serviceUrl，任务能跑但**表单上改的值一律不生效**
                   （全部落回 Java 默认值）。

本补丁只加 LINEAGE_DAG 分支（LINEAGE 的表单不在本次改动范围内，保持原状，见 README 7.8 的说明）：
映射的 4 个字段与前端表单 LDF、Java 类 LineageDagParameters 一一对应；
**不映射 timeout** —— 表单里那个 timeout 是通用段的「任务超时（分钟）」，而 Java 的
LineageDagParameters.timeout 是「HTTP 超时（毫秒）」，语义不同，映射过去会把 HTTP 超时设成 30ms。

用法: python3 apps/apps/ds-plugin/verify/patch_modal_taskparams.py [detail-modal.<hash>.js]
"""

import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT = os.path.join(HERE, '..', 'frontend', 'ui-static', 'assets', 'detail-modal.de80dd20.js')

ANCHOR = 'r.listParameters=e.listParameters);let u=""'
BRANCH = ('r.listParameters=e.listParameters),'
          'e.taskType==="LINEAGE_DAG"&&(r.scope=e.scope,r.taskTypes=e.taskTypes,'
          'r.includeSubProcess=e.includeSubProcess,r.serviceUrl=e.serviceUrl);let u=""')
MARK = 'e.taskType==="LINEAGE_DAG"&&(r.scope=e.scope'


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    text = open(path, encoding='utf-8').read()
    if MARK in text:
        print('已是补丁状态，无需改动: %s' % os.path.relpath(path, REPO))
        return
    if text.count(ANCHOR) != 1:
        raise SystemExit('!! 锚点 %r 出现 %d 次（预期 1 次），海豚版本可能变了'
                         % (ANCHOR, text.count(ANCHOR)))
    new = text.replace(ANCHOR, BRANCH)
    shutil.copy2(path, os.path.join(HERE, '..', 'java', 'build',
                                    os.path.basename(path) + '.pre-taskparams.bak'))
    open(path, 'w', encoding='utf-8').write(new)
    print('已写入 %s' % os.path.relpath(path, REPO))
    print('  插入: %s' % MARK)
    print('  size: %d -> %d' % (len(text.encode()), len(new.encode())))


if __name__ == '__main__':
    main()
