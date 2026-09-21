#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 detail.<hash>.js 打「LINEAGE_DAG 专属节点设置表单」补丁（幂等）。

改动只有两处（与镜像原版逐字符 diff 也就这两处）：
  1. 在 `const oa={` 之前插入专属表单函数 `function LDF({projectCode,from,readonly,data}){…}`
  2. 把映射表里的 `,LINEAGE_DAG:Rr}`（Rr = SQL 的表单函数）改成 `,LINEAGE_DAG:LDF}`

`LINEAGE:Rr` 保持不动 —— 单脚本血缘确实需要 SQL 输入框。
通用段（节点名称/执行标志/描述/优先级/Worker/环境/任务组/超时/前置任务）照抄 SUB_PROCESS（`Er`）的写法，
只在中间夹入 4 个业务字段（scope / taskTypes / includeSubProcess / serviceUrl）+ 一条提示文本。

用法: python3 apps/apps/ds-plugin/verify/patch_detail_ldf.py [detail.<hash>.js]
"""

import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT = os.path.join(HERE, '..', 'frontend', 'ui-static', 'assets', 'detail.f1164795.js')

LDF = (
    'function LDF({projectCode:e,from:t=0,readonly:n,data:a}){'
    'const r=h({taskType:"LINEAGE_DAG",name:"",flag:"YES",description:"",timeoutFlag:!1,'
    'localParams:[],environmentCode:null,failRetryInterval:1,failRetryTimes:0,'
    'workerGroup:"default",delayTime:0,timeout:30,timeoutNotifyStrategy:["WARN"],'
    'scope:"current",taskTypes:"SQL,SHELL,PYTHON",includeSubProcess:!1,'
    'serviceUrl:"http://172.17.0.1:18080"});'
    'return{json:[k(t),...O({projectCode:e,from:t,readonly:n,data:a,model:r}),'
    'L(),N(),S(),E(e),w(r,!(a!=null&&a.id)),...R(r,e),...A(r),'
    # ---- LINEAGE_DAG 专属业务字段（只有这 4 个 + 一条提示）----
    '{type:"custom",field:"lineageDagTips",span:24,'
    'widget:B("div",{style:{color:"#86909c","font-size":"12px","line-height":"20px","padding":"4px 0"}},'
    '"LINEAGE_DAG · 工作流级血缘：无需填写 SQL / 数据源，任务运行时自动拉取工作流内的任务脚本并解析；'
    '参数为 scope / taskTypes / includeSubProcess / serviceUrl。")},'
    '{type:"select",field:"scope",span:12,name:"解析范围 (scope)",'
    'options:[{label:"当前工作流（current）",value:"current"},{label:"整个项目（project）",value:"project"}],'
    'validate:{trigger:["input","blur"],required:!0}},'
    '{type:"input",field:"taskTypes",span:12,name:"解析任务类型 (taskTypes)",'
    'props:{placeholder:"SQL,SHELL,PYTHON",maxLength:200},'
    'validate:{trigger:["input","blur"],required:!0,message:"解析任务类型不能为空，例如 SQL,SHELL,PYTHON"}},'
    '{type:"switch",field:"includeSubProcess",span:12,name:"包含子工作流 (includeSubProcess)"},'
    '{type:"input",field:"serviceUrl",span:12,name:"血缘服务地址 (serviceUrl)",'
    'props:{placeholder:"http://172.17.0.1:18080",maxLength:200},'
    'validate:{trigger:["input","blur"],required:!0,message:"血缘服务地址不能为空"}},'
    # ---- 通用段收尾 ----
    'P()],model:r}}'
)
OLD_MAP = ',LINEAGE_DAG:Rr}'
NEW_MAP = ',LINEAGE_DAG:LDF}'
ANCHOR = 'const oa={'


def md5b(b):
    return hashlib.md5(b).hexdigest()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    src = open(path, 'rb').read()
    text = src.decode('utf-8')          # 非 UTF-8 会直接抛，避免把文件写坏

    assert text.count(OLD_MAP) <= 1, 'OLD_MAP 出现多次'
    assert text.count(ANCHOR) == 1, 'ANCHOR 出现 %d 次（预期 1 次）' % text.count(ANCHOR)

    changed = []
    if OLD_MAP in text:
        text = text.replace(OLD_MAP, NEW_MAP)
        changed.append('oa 映射: LINEAGE_DAG:Rr -> LINEAGE_DAG:LDF')
    elif NEW_MAP in text:
        changed.append('oa 映射已是 LDF（跳过）')
    else:
        raise SystemExit('!! 既找不到 %r 也找不到 %r' % (OLD_MAP, NEW_MAP))

    if 'function LDF(' in text:
        changed.append('LDF 已存在（跳过插入）')
    else:
        text = text.replace(ANCHOR, LDF + ANCHOR, 1)
        changed.append('已插入 function LDF(...) 于 %s 之前' % ANCHOR)

    out = text.encode('utf-8')
    if md5b(out) == md5b(src):
        print('无变化: %s' % os.path.relpath(path, REPO))
    else:
        shutil.copy2(path, os.path.join(HERE, '..', 'java', 'build',
                                        os.path.basename(path) + '.pre-ldf.bak'))
        open(path, 'wb').write(out)
        print('已写入 %s' % os.path.relpath(path, REPO))
    for c in changed:
        print('  -', c)
    print('  size: %d -> %d  md5: %s -> %s' % (len(src), len(out), md5b(src), md5b(out)))


if __name__ == '__main__':
    main()
