#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「表单 → 保存 payload」全链路真跑验证（QuickJS，不需要浏览器）。

为什么需要它
------------
节点设置弹窗有两段前端逻辑，分别在两个 bundle 里：

  1. `detail.<hash>.js`        —— 表单函数（`LDF`）产出 `{json, model}`，决定**弹窗长什么样**；
  2. `detail-modal.<hash>.js`  —— 保存函数 `Ne(model)` 拼 `taskParams`，决定**用户填的值能不能存下去**。

`Ne` 里有一串 `e.taskType==="XXX"&&(r.字段=e.字段,…)` 的白名单 if 链，只有列进去的字段才会进
`taskParams`（`{localParams,initScript,rawScript,resourceList:[],...r}`）。**原版没有 LINEAGE_DAG
分支**，于是表单上填的 scope/taskTypes/includeSubProcess/serviceUrl 会被静默丢掉、全部落回 Java 默认值。

本脚本把这两个 bundle 里的**真实函数**抽出来串起来跑：

    LDF(真实)  →  model  →  Ne(真实, 原版)   →  taskParams（应当丢字段 = 复现问题）
                          →  Ne(真实, 补丁后) →  taskParams（应当带 4 个字段 = 修复生效）

用法
----
    .venv/bin/python apps/apps/ds-plugin/verify/verify_save_params.py            # 跑通即 ALL_PASS
    .venv/bin/python apps/apps/ds-plugin/verify/verify_save_params.py --keep
"""

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import verify_ldf_form as V  # noqa: E402  （复用抽取器 + 原版文件缓存）

DEFAULT_MODAL = os.path.join(HERE, '..', 'frontend', 'ui-static', 'assets', 'detail-modal.de80dd20.js')

HARNESS_TAIL = r'''
/* ================= 真跑：表单 → model → 保存 payload ================= */
var model = LD.LDF({ projectCode: 1, from: 0, readonly: false, data: {} }).model;
/* 弹窗保存时会补的通用字段（与 detail-modal 里一致） */
model.preTasks = [0];
model.taskExecuteType = "BATCH";
model.name = "t_dag_工作流血缘";
model.description = "工作流级血缘分析";

var before = NE_OLD.Ne(model);          /* 原版保存函数 */
var after  = NE_NEW.Ne(model);          /* 打过补丁的保存函数 */

var pb = before.taskDefinitionJsonObj.taskParams;
var pa = after.taskDefinitionJsonObj.taskParams;
var fails = [];
function ok(cond, msg){ if (cond) say("PASS  " + msg); else { fails.push(msg); say("FAIL  " + msg); } }

say("=== [1] LDF 真跑出来的 model（表单初值）===");
say(JSON.stringify({scope:model.scope, taskTypes:model.taskTypes,
                    includeSubProcess:model.includeSubProcess, serviceUrl:model.serviceUrl}));
say("");
say("=== [2] 原版 Ne() 生成的 taskParams ===");
say("键 = [" + Object.keys(pb).join(",") + "]");
say("值 = " + JSON.stringify(pb));
say("");
say("=== [3] 补丁后 Ne() 生成的 taskParams ===");
say("键 = [" + Object.keys(pa).join(",") + "]");
say("值 = " + JSON.stringify(pa));
say("");
say("=== [4] 断言 ===");
var BIZ = ["scope","taskTypes","includeSubProcess","serviceUrl"];
var lost = [];
for (var i=0;i<BIZ.length;i++) if (!(BIZ[i] in pb)) lost.push(BIZ[i]);
ok(lost.length === BIZ.length,
   "复现问题：原版保存会把 4 个业务字段**全部丢掉**（实际丢 " + lost.length + " 个: " + lost.join(",") + "）");
var kept = [];
for (var i2=0;i2<BIZ.length;i2++) if (BIZ[i2] in pa) kept.push(BIZ[i2]);
ok(kept.length === BIZ.length,
   "修复生效：补丁后 taskParams 里 4 个业务字段齐全（实际 " + kept.length + " 个: " + kept.join(",") + "）");
ok(pa.scope === "current", "taskParams.scope = current");
ok(pa.taskTypes === "SQL,SHELL,PYTHON", "taskParams.taskTypes = SQL,SHELL,PYTHON");
ok(pa.includeSubProcess === false, "taskParams.includeSubProcess = false");
ok(pa.serviceUrl === "http://172.17.0.1:18080", "taskParams.serviceUrl = http://172.17.0.1:18080");
ok(!("timeout" in pa), "没有把通用段的 timeout（分钟）错映射成 HTTP 超时");
ok(!("sql" in pa) && !("datasource" in pa) && !("sqlType" in pa),
   "taskParams 里没有 sql / datasource / sqlType");
ok(pa.localParams instanceof Array && pa.resourceList instanceof Array,
   "通用字段仍在（localParams / resourceList）");
ok(after.taskDefinitionJsonObj.taskType === "LINEAGE_DAG", "任务定义 taskType = LINEAGE_DAG");

/* 交叉验证：model 里改了值，保存后必须跟着变（不是写死的默认值） */
var m2 = LD.LDF({ projectCode: 1, from: 0, readonly: false, data: {} }).model;
m2.scope = "project"; m2.taskTypes = "SQL"; m2.includeSubProcess = true;
m2.serviceUrl = "http://10.0.0.9:18080"; m2.preTasks = []; m2.name = "x";
var p2 = NE_NEW.Ne(m2).taskDefinitionJsonObj.taskParams;
ok(p2.scope === "project" && p2.taskTypes === "SQL" && p2.includeSubProcess === true
   && p2.serviceUrl === "http://10.0.0.9:18080",
   "用户改过的值能存下去（scope=project / taskTypes=SQL / includeSubProcess=true / serviceUrl=10.0.0.9）");

say("");
say(fails.length === 0 ? "RESULT: ALL_PASS（全部断言通过）"
                       : ("RESULT: FAILED(" + fails.length + "): " + fails.join(" || ")));
for (var n=0;n<__log.length;n++) print(__log[n]);
'''


def modal_ne_iife(modal_src, var_name):
    """抽真实 `Ne(e)`（拼 taskParams 的白名单函数）并包进独立作用域。"""
    ne = V.fdecl(modal_src, 'Ne')
    return '\n'.join([
        'var %s = (function(){' % var_name,
        'function N(v){ return { value: v }; }',   # detail-modal 里 `k as N` 就是 ref
        ne,
        'return { Ne: Ne };',
        '})();',
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--modal', default=DEFAULT_MODAL, help='补丁后的 detail-modal.<hash>.js')
    ap.add_argument('--bundle', default=V.DEFAULT_BUNDLE, help='补丁后的 detail.<hash>.js')
    ap.add_argument('--stock-dir', default=V.DEFAULT_STOCK_DIR)
    ap.add_argument('--image', default=V.DEFAULT_IMAGE)
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()

    V.ensure_stock(args.stock_dir, args.image)
    stock_modal = os.path.join(args.stock_dir, 'detail-modal.de80dd20.js')
    if not os.path.exists(stock_modal):
        # 缓存里没有就现取（保持与其它原版文件同一目录）
        V.STOCK_FILES.append('detail-modal.de80dd20.js')
        V.ensure_stock(args.stock_dir, args.image)

    det = open(args.bundle, encoding='utf-8').read()
    mod = open(os.path.join(args.stock_dir, 'index.module.ae0f5683.js'), encoding='utf-8').read()
    new_modal = open(args.modal, encoding='utf-8').read()
    old_modal = open(stock_modal, encoding='utf-8').read()

    print('bundle(detail) : %s (%d bytes, md5=%s)' % (
        os.path.relpath(args.bundle, REPO), os.path.getsize(args.bundle),
        hashlib.md5(open(args.bundle, 'rb').read()).hexdigest()))
    print('modal(补丁后)  : %s (%d bytes, md5=%s)' % (
        os.path.relpath(args.modal, REPO), os.path.getsize(args.modal),
        hashlib.md5(open(args.modal, 'rb').read()).hexdigest()))
    print('modal(原版)    : %s' % os.path.relpath(stock_modal, REPO))
    print('-' * 78)

    lines = ['var __log = []; function say(x){ __log.push(String(x)); }']
    lines.append(modal_ne_iife(old_modal, 'NE_OLD'))
    lines.append(modal_ne_iife(new_modal, 'NE_NEW'))
    lines.append(V.ldf_iife(det, mod, 'LDF'))
    lines.append(HARNESS_TAIL)
    js = '\n'.join(lines)

    out_dir = os.path.join(HERE, 'build')
    os.makedirs(out_dir, exist_ok=True)
    js_path = os.path.join(out_dir, 'verify_save_params.harness.js')
    open(js_path, 'w', encoding='utf-8').write(js)
    print('harness: %s (%d 字符)' % (os.path.relpath(js_path, REPO), len(js)))

    rc, passed = V.run_harness(js_path, expect_pass=True)
    if not args.keep and os.path.exists(js_path):
        os.remove(js_path)
    return rc


if __name__ == '__main__':
    sys.exit(main())
