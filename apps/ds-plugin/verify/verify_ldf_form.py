#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINEAGE_DAG 专属表单 —— 「渲染函数真跑」验证（在 JS 引擎里真的执行一遍）。

为什么需要它
------------
海豚 3.2.2 的前端把「任务类型 -> 节点设置表单函数」的映射表写死在 bundle
`ui/assets/detail.<hash>.js` 里：

    oa = { SHELL:Sr, SUB_PROCESS:Er, ..., SQL:Rr, LINEAGE:Rr, LINEAGE_DAG:Rr }

`oa[taskType]` 返回 `{json:[组件描述...], model:参数模型}`，再由
`get-elements-by-json` 的 `We(json, model)` 渲染成表单。所以「表单长什么样」这件事
可以用纯 JS 断言，不需要浏览器。

本脚本做的事
------------
1. 从**归档的补丁文件** `ui-static/assets/detail.f1164795.js` 里把 LINEAGE_DAG 的
   表单函数（`LDF`）**原样抽出**，连同它真实调用的依赖：
     - 同文件内的 k/O/L/N/R/A/P（通用段：节点名/执行标志/描述/优先级/Worker/环境/任务组/超时/前置任务）
     - index.module.ae0f5683.js 的 S/E/w（任务优先级 / Worker 分组 / 环境，原样抽取后改名）
     - get-elements-by-json.8a8b3614.js 的渲染管线 We()（原样抽取）
   只 mock 各模块 import 进来的 Vue / i18n / api 原语（ref/computed/onMounted/reactive/组件…）。
2. 在 QuickJS 里真跑 `LDF({projectCode,from,readonly,data})`，断言：
     - 返回 {json:[…], model:{…}}
     - json 里**没有** field='sql'/'sqlType'/'datasource'/'type'/'displayRows'/'connParams'/…
       也没有 type='editor'（SQL 编辑器）
     - model 里有 scope/taskTypes/includeSubProcess/serviceUrl 及默认值
     - scope 是 select 且选项 current/project；includeSubProcess 是 switch
     - 通用段仍在（name/flag/description/taskPriority/workerGroup/environmentCode/taskGroupId/timeout/preTasks）
3. 把 json 再喂给真实的渲染管线 `We(json, model)`，断言渲染出的 elements 里
   同样没有 sql/数据源路径、且 4 个业务字段都在。
4. `--control` 模式：用**同一个可执行断言**去跑 SQL 表单函数 `Rr`（= 修复前
   LINEAGE_DAG 复用的那个），它必须大面积 FAIL —— 用来证明这套断言不是空转。

用法
----
    .venv/bin/python apps/apps/ds-plugin/verify/verify_ldf_form.py            # 验证补丁后的 LDF
    .venv/bin/python apps/apps/ds-plugin/verify/verify_ldf_form.py --control  # 对照：SQL 表单 Rr（应 FAIL）
    .venv/bin/python apps/apps/ds-plugin/verify/verify_ldf_form.py --keep     # 保留生成的 harness 便于阅读
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_BUNDLE = os.path.join(HERE, '..', 'frontend', 'ui-static', 'assets', 'detail.f1164795.js')
DEFAULT_STOCK_DIR = os.path.join(HERE, '..', 'java', 'build', 'stock_ui')
DEFAULT_IMAGE = 'apache/dolphinscheduler-standalone-server:3.2.2'
STOCK_FILES = ['detail.f1164795.js', 'index.module.ae0f5683.js', 'get-elements-by-json.8a8b3614.js']
UI_ASSETS = '/opt/dolphinscheduler/ui/assets'


# --------------------------------------------------------------------------- #
# 源码抽取
# --------------------------------------------------------------------------- #
def _match(s, i, o, c):
    depth = 0
    instr = None
    esc = False
    while i < len(s):
        ch = s[i]
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == instr:
                instr = None
        else:
            if ch in '"\'`':
                instr = ch
            elif ch == o:
                depth += 1
            elif ch == c:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError('unbalanced %s at %d' % (o, i))


def fdecl(src, name):
    m = re.search(r'function ' + re.escape(name) + r'\(', src)
    if not m:
        raise KeyError('function %s( 不存在' % name)
    st = m.start()
    p = src.index('(', st)
    pe = _match(src, p, '(', ')')
    b = src.index('{', pe)
    return src[st:_match(src, b, '{', '}') + 1]


def constdecl(src, name):
    m = re.search(r'const ' + re.escape(name) + r'\s*=', src)
    if not m:
        raise KeyError('const %s= 不存在' % name)
    st = m.start()
    i = m.end()
    instr = None
    esc = False
    depth = 0
    while i < len(src):
        ch = src[i]
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == instr:
                instr = None
        else:
            if ch in '"\'`':
                instr = ch
            elif ch in '([{':
                depth += 1
            elif ch in ')]}':
                depth -= 1
            elif ch == ';' and depth == 0:
                return src[st:i + 1]
        i += 1
    raise ValueError('const %s 没有找到分号' % name)


def strip_esm(src):
    src = re.sub(r'import\s*\{[^}]*\}\s*from\s*"[^"]*";', '', src)
    src = re.sub(r'import\s*"[^"]*";', '', src)
    return re.sub(r'export\s*\{[^}]*\};?', '', src)


def rename_decl(txt, old, new):
    """"function L(" -> "function _L(" """
    assert txt.startswith(old)
    return new + txt[len(old):]


# --------------------------------------------------------------------------- #
# 从基础镜像里取「原版」文件（对照实验 / 依赖所在模块）
# --------------------------------------------------------------------------- #
def ensure_stock(stock_dir, image, force=False):
    os.makedirs(stock_dir, exist_ok=True)
    for f in STOCK_FILES:
        dst = os.path.join(stock_dir, f)
        if os.path.exists(dst) and not force:
            continue
        cmd = ('cat %s/%s' % (UI_ASSETS, f))
        with open(dst, 'wb') as fh:
            r = subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'sh', image, '-c', cmd],
                               stdout=fh)
        if r.returncode != 0 or os.path.getsize(dst) == 0:
            raise SystemExit('!! 无法从镜像 %s 取出 %s（可先 `docker pull %s`）' % (image, f, image))
    return stock_dir


# --------------------------------------------------------------------------- #
# 生成 harness
# --------------------------------------------------------------------------- #
MODULE_IIFE = r'''
var I18N1 = {"project.node.task_priority":"任务优先级","project.node.worker_group":"Worker 分组",
 "project.node.worker_group_tips":"请选择 Worker 分组","project.node.environment_name":"环境名称"};
function i(){ return { t: function(k){ return (k in I18N1) ? I18N1[k] : k; } }; }
function y(v){ return { value: v }; }
function c(v){ return { value: v }; }
function p(type, props, children){ return { __vnode:true, type:type, props:props||null, children:children===undefined?null:children }; }
function d(fn){ return; }
function _(a,b,cc){ return; }
function b(pc){ return Promise.resolve({ data:[{ workerGroup:"default" }] }); }
function G(){ return Promise.resolve([{ name:"prod", code:7, workerGroups:["default"] }]); }
var k = {}, f = {}, m = {};
'''

DETAIL_MOCKS = r'''
var I18N2 = {
 "project.node.task_name":"任务名称","project.node.name":"节点名称","project.node.name_tips":"请输入节点名称",
 "project.node.run_flag":"执行标志","project.node.normal":"正常","project.node.prohibition_execution":"禁止执行",
 "project.node.description":"描述","project.node.description_tips":"请输入描述",
 "project.node.task_group_name":"任务组名称","project.node.task_group_queue_priority":"任务组队列优先级",
 "project.node.timeout_alarm":"超时告警","project.node.timeout_failure":"超时失败",
 "project.node.timeout_strategy":"超时策略","project.node.timeout_strategy_tips":"请选择超时策略",
 "project.node.timeout_period":"超时时长","project.node.timeout_period_tips":"请输入超时时长",
 "project.node.minute":"分","project.node.pre_tasks":"前置任务"};
function y(){ return { t: function(k){ return (k in I18N2) ? I18N2[k] : k; } }; }   /* useLocale */
function i(v){ return { value: v }; }        /* ref */
function T(fn){ return { value: fn() }; }    /* computed */
function G(fn){ return; }                    /* onMounted -> 不执行（避开 api 请求） */
function F(a,b,c2){ return; }                /* watch -> 不执行副作用 */
function h(o){ return o; }                   /* reactive（真身是 Vue reactive，这里恒等） */
function B(type, props, children){ return { __vnode:true, type:type, props:props||null, children:children===undefined?null:children }; }
async function Fe(){ return { totalList: [] }; }
async function Ge(p){ return []; }
function $(){ return { getPreTaskOptions:[], getPreTasks:[], postTaskOptions:[],
  getName:function(){return "";}, updateName:function(){}, updateDefinition:function(){} }; }
function le(){ return { currentRoute:{ value:{ params:{ code:"0" } } } }; }
var z = {};
function qe(){ return null; } function Oe(x){ return x; } function xe(){ return null; }
function oe(x){ return x; } function se(f){ return { value:undefined }; }
function Z_(){ return { type:"custom", field:"__resources__" }; }
function X_(){ return { type:"custom", field:"__varpool__" }; }
'''

GE_MOCKS = r'''
var lodash = { exports: {
  isFunction: function(x){ return typeof x === "function"; },
  omit: function(o, keys){ var r={}; for (var kk in o){ if (keys.indexOf(kk)===-1) r[kk]=o[kk]; } return r; },
  upperFirst: function(s2){ return s2.charAt(0).toUpperCase()+s2.slice(1); },
  camelCase: function(s2){ return s2.replace(/[-_ ](\w)/g, function(_,c2){ return c2.toUpperCase(); }); }
}};
function vnode(type, props, children){ return { __vnode:true, type:type, props:props||null, children:children===undefined?null:children }; }
function toValue(x){ return (x && typeof x === "object" && "value" in x) ? x.value : x; }
function isVNode(x){ return !!(x && x.__vnode); }
function getProp(o,p){ return o==null ? undefined : o[p]; }
function refM(v){ return { value:v }; }
function assign(a,b){ var r={}; for (var kk in a) r[kk]=a[kk]; for (var k2 in b) r[k2]=b[k2]; return r; }
var stub = function(){ return undefined; };
var d = lodash, s = vnode, N = vnode, O = assign, f = toValue, T = isVNode, L = getProp, E = refM,
    b = stub, G = stub, q = stub, z = stub, V = stub, w = stub, B = stub, K = stub, C = stub, H = stub,
    J = stub, I = stub, Y = stub, D = stub, P = stub, x = stub, Q = stub, W = stub, k = stub, _ = stub,
    v = stub, X = stub, Z = stub, ee = stub, $ = assign, te = stub, oe = stub, re = stub, j = stub,
    ne = stub, se = stub, a = stub;
'''

CHECK_SECTION = r'''
/* ================= 真跑 ================= */
var ctxCreate = { projectCode: 1, from: 0, readonly: false, data: {} };
var out = LD.TARGET(ctxCreate);
var json = out.json, model = out.model;
var fails = [];
function ok(cond, msg){ if (cond) say("PASS  " + msg); else { fails.push(msg); say("FAIL  " + msg); } }
function fieldList(j){ var r=[]; for (var n=0;n<j.length;n++) r.push(j[n] ? j[n].field : null); return r; }

say("=== [1] TARGET({projectCode:1,from:0,...}) 真跑返回 ===");
say("json 元素数 = " + json.length);
say("model = " + JSON.stringify(model));
say("");
say("=== [2] 表单元素清单（type | field | label）===");
for (var n=0;n<json.length;n++){
  var e = json[n];
  if (!e) { say(" [" + n + "] (null/undefined)"); continue; }
  say(" [" + (n<10?" ":"") + n + "] type=" + e.type + " | field=" + e.field +
      " | label=" + (e.name===undefined?"(无)":e.name) +
      (e.validate && e.validate.required ? " | required" : ""));
}
say("");
say("=== [3] 断言 A：返回结构 / 无 SQL 字段 / model 默认值 ===");
ok(json instanceof Array && json.length > 0, "返回对象的 json 是数组且非空 (length=" + json.length + ")");
ok(model !== null && typeof model === "object", "返回对象的 model 是对象");
var BAD = ["sql","sqlType","datasource","type","displayRows","connParams","preStatements","postStatements","udfs"];
var fds = fieldList(json), hits = [];
for (var n2=0;n2<fds.length;n2++) if (BAD.indexOf(fds[n2]) >= 0) hits.push(fds[n2]);
ok(hits.length === 0, "json 里不含 SQL/数据源专属字段 (" + BAD.join("/") + ") 实际命中=[" + hits.join(",") + "]");
var hasEditor = false;
for (var n3=0;n3<json.length;n3++) if (json[n3] && json[n3].type === "editor") hasEditor = true;
ok(!hasEditor, "json 里不含 SQL 编辑器组件 (type='editor')");
ok(fds.indexOf("scope") >= 0, "含业务字段 scope");
ok(fds.indexOf("taskTypes") >= 0, "含业务字段 taskTypes");
ok(fds.indexOf("includeSubProcess") >= 0, "含业务字段 includeSubProcess");
ok(fds.indexOf("serviceUrl") >= 0, "含业务字段 serviceUrl");
ok(json[0] && json[0].field === "name", "第 1 个元素仍是通用段「节点名称」");
ok(fds.indexOf("description") >= 0 && fds.indexOf("flag") >= 0 && fds.indexOf("preTasks") >= 0
   && fds.indexOf("timeout") >= 0 && fds.indexOf("taskPriority") >= 0 && fds.indexOf("workerGroup") >= 0
   && fds.indexOf("environmentCode") >= 0 && fds.indexOf("taskGroupId") >= 0,
   "通用段完整（节点名称/描述/执行标志/优先级/Worker分组/环境/任务组/超时/前置任务）");
ok(model.scope === "current", "model.scope 默认值 = current");
ok(model.taskTypes === "SQL,SHELL,PYTHON", "model.taskTypes 默认值 = SQL,SHELL,PYTHON");
ok(model.includeSubProcess === false, "model.includeSubProcess 默认值 = false");
ok(model.serviceUrl === "http://172.17.0.1:18080", "model.serviceUrl 默认值 = http://172.17.0.1:18080");
ok(model.taskType === "LINEAGE_DAG", "model.taskType = LINEAGE_DAG");
var sd=null, id2=null, td=null, ud=null;
for (var n4=0;n4<json.length;n4++){
  if (!json[n4]) continue;
  if (json[n4].field==="scope") sd=json[n4];
  if (json[n4].field==="includeSubProcess") id2=json[n4];
  if (json[n4].field==="taskTypes") td=json[n4];
  if (json[n4].field==="serviceUrl") ud=json[n4];
}
ok(sd && sd.type === "select", "scope 渲染成下拉 (type='select')");
var ov=[]; if (sd && sd.options) for (var n5=0;n5<sd.options.length;n5++) ov.push(sd.options[n5].value);
ok(ov.join(",") === "current,project", "scope 选项 = current/project 实际=[" + ov.join(",") + "]");
ok(id2 && id2.type === "switch", "includeSubProcess 渲染成开关 (type='switch')");
ok(td && ud && td.type === "input" && ud.type === "input", "taskTypes / serviceUrl 渲染成输入框 (type='input')");

say("");
say("=== [4] 断言 B：把 json 喂进真实渲染管线 GE.We(json, model) ===");
try {
  var rendered = GE.We(json, model);
  say("elements 数 = " + rendered.elements.length);
  say("rules 键 = [" + Object.keys(rendered.rules).join(",") + "]");
  say("initialValues = " + JSON.stringify(rendered.initialValues));
  var paths=[], labels=[], allFn=true;
  for (var n6=0;n6<rendered.elements.length;n6++){
    var el = rendered.elements[n6];
    paths.push(el.path===undefined?"":String(el.path));
    labels.push(el.label===undefined?"":String(el.label));
    if (typeof el.widget !== "function") allFn = false;
  }
  say("渲染 path  = " + JSON.stringify(paths));
  say("渲染 label = " + JSON.stringify(labels));
  ok(rendered.elements.length === json.length, "每个 json 元素都渲染成 1 个 element (" + json.length + " 个)");
  ok(paths.indexOf("sql")<0 && paths.indexOf("datasource")<0 && paths.indexOf("sqlType")<0 && paths.indexOf("type")<0,
     "渲染后 path 里没有 sql/datasource/sqlType/type");
  ok(paths.indexOf("scope")>=0 && paths.indexOf("taskTypes")>=0 && paths.indexOf("includeSubProcess")>=0 && paths.indexOf("serviceUrl")>=0,
     "渲染后 path 里有 scope/taskTypes/includeSubProcess/serviceUrl");
  ok(allFn, "每个 element 的 widget 都是可渲染函数（element type 全在支持列表 " + JSON.stringify(GE.supported) + " 内）");
} catch (err) { say("渲染管线抛错: " + err); fails.push("渲染管线 GE.We() 未跑通: " + err); }

say("");
say(fails.length === 0 ? "RESULT: ALL_PASS（全部断言通过）" : ("RESULT: FAILED(" + fails.length + "): " + fails.join(" || ")));
for (var n7=0;n7<__log.length;n7++) print(__log[n7]);
'''


def ldf_iife(det, mod, target='LDF'):
    """只产出 `LDF` 所在的 detail 模块作用域（`var LD = (function(){…})();`）。

    给 verify_save_params.py 复用：那里要「先跑 LDF 拿到 model，再把 model 喂给保存函数
    Ne() 算 taskParams」，所以不需要本文件里的断言段。
    注意：S/E/w 来自 index.module，与 detail 模块的同名 import 语义不同（i/y 正好反着），
    必须各自包在独立 IIFE 里，不能合到一个作用域。
    """
    ld = {n: fdecl(det, n) for n in ['k', 'L', 'N', 'R', 'A', 'P', target]}
    ld['O'] = constdecl(det, 'O')
    out = ['var __MOD = (function(){', MODULE_IIFE,
           'var S = ' + rename_decl(fdecl(mod, 'I'), 'function I', 'function S') + ';',
           'var E = ' + rename_decl(fdecl(mod, 'C'), 'function C', 'function E') + ';',
           'var w = ' + rename_decl(fdecl(mod, 'H'), 'function H', 'function w') + ';',
           'return { S:S, E:E, w:w };', '})();',
           'var LD = (function(){', DETAIL_MOCKS, 'var Z = Z_, X = X_;']
    for n in ['k', 'O', 'L', 'N', 'R', 'A', 'P']:
        out.append(ld[n])
    out.append('var S = __MOD.S, E = __MOD.E, w = __MOD.w;')
    out.append(ld[target])
    out.append('return { %s: %s };' % (target, target))
    out.append('})();')
    return '\n'.join(out)


def build_harness(bundle_path, stock_dir, target, control):
    det = open(bundle_path, encoding='utf-8').read()
    mod = open(os.path.join(stock_dir, 'index.module.ae0f5683.js'), encoding='utf-8').read()
    ge_src = open(os.path.join(stock_dir, 'get-elements-by-json.8a8b3614.js'), encoding='utf-8').read()

    ld_names = ['k', 'L', 'N', 'R', 'A', 'P', target]
    ld = {n: fdecl(det, n) for n in ld_names}
    ld['O'] = constdecl(det, 'O')

    extra = []
    if control:
        # SQL 表单 Rr 需要的专属 helper：全部原样抽取（含 SQL 编辑器 / 数据源 / sqlType）
        # 携带 sql / datasource / sqlType / editor 的元素定义全部原样抽取
        for n in ['D', 'C', 'K', 'Ze', 'St']:
            extra.append((n, fdecl(det, n)))
        extra.append(('Xe', constdecl(det, 'Xe')))

    mod_l = rename_decl(fdecl(mod, 'L'), 'function L', 'function _L')
    mod_m = rename_decl(fdecl(mod, 'M'), 'function M', 'function _M')
    mod_s = rename_decl(fdecl(mod, 'I'), 'function I', 'function S')
    mod_e = rename_decl(fdecl(mod, 'C'), 'function C', 'function E')
    mod_w = rename_decl(fdecl(mod, 'H'), 'function H', 'function w')
    ge_body = strip_esm(ge_src)

    out = []
    add = out.append
    add('/* ===== 自动生成（apps/apps/ds-plugin/verify/verify_ldf_form.py）：%s 真跑验证 ===== */'
        % ('SQL 表单 Rr 对照实验' if control else 'LINEAGE_DAG 表单 LDF'))
    add('var __log = []; function say(x){ __log.push(String(x)); }')

    add('/* --- 模块 index.module.ae0f5683.js：真实 S(优先级)/E(Worker)/w(环境)，仅 mock 其 import --- */')
    add('var MOD = (function(){')
    add(MODULE_IIFE)
    add('var _L = ' + mod_l + ';')
    add('var _M = ' + mod_m + ';')
    add('var S = ' + mod_s + ';')
    add('var E = ' + mod_e + ';')
    add('var w = ' + mod_w + ';')
    add('return { S:S, E:E, w:w, L:_L, M:_M };')
    add('})();')

    add('')
    add('/* --- 模块 detail.<hash>.js：真实 %s（仅 mock 其 import 的 vue/i18n/api 原语）--- */'
        % (' / '.join(['k', 'O', 'L', 'N', 'R', 'A', 'P']) + ' / ' + target))
    add('var LD = (function(){')
    add(DETAIL_MOCKS)
    add('var Z = Z_, X = X_;')
    if control:
        # 这两个 helper 与非 SQL 字段有关（自定义参数表格 / UDF 导入按钮），用最小桩替代；
        # 凡携带 sql / datasource / sqlType / editor 的元素定义都是原样抽取的。
        add("var q = function(o){ return [{ type:'custom-parameters', field:o.field }]; };")
        add("var ht = function(e){ return { type:'custom', field:'__udf_import' }; };")
    for n in ['k', 'O', 'L', 'N', 'R', 'A', 'P']:
        add(ld[n])
    if control:
        for name, body in extra:
            add(body)
    add('var S = MOD.S, E = MOD.E, w = MOD.w;')
    add('var M = MOD.L, Q = MOD.M;')
    add(ld[target])
    add('return { %s: %s };' % (target, target))
    add('})();')

    add('')
    add('/* --- 模块 get-elements-by-json.<hash>.js：真实渲染管线 We() --- */')
    add('var GE = (function(){')
    add(GE_MOCKS)
    add(ge_body)
    add('return { We: We, dispatcher: A, supported: Re };')
    add('})();')
    add(CHECK_SECTION.replace('TARGET', target))
    return '\n'.join(out)


# --------------------------------------------------------------------------- #
def run_harness(js_path, expect_pass=True):
    """返回 (退出码, 是否 ALL_PASS)。"""
    try:
        import quickjs
    except ImportError:
        raise SystemExit('!! 需要 quickjs：/root/projects/sql-lineage-mvp/.venv/bin/python 里已装；'
                         '或 pip install quickjs')
    lines = []
    src = open(js_path, encoding='utf-8').read()
    ctx = quickjs.Context()
    ctx.add_callable('print', lambda s: lines.append(str(s)))
    try:
        ctx.eval(src)
    except Exception as e:  # noqa: BLE001
        for ln in lines:
            print(ln)
        print('!! harness 执行失败: %s' % str(e)[:2000])
        return 2, False
    for ln in lines:
        print(ln)
    passed = any(ln.startswith('RESULT: ALL_PASS') for ln in lines)
    if passed == expect_pass:
        return 0, passed
    return 1, passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE, help='补丁后的 detail.<hash>.js')
    ap.add_argument('--stock-dir', default=DEFAULT_STOCK_DIR, help='原版依赖文件缓存目录')
    ap.add_argument('--image', default=DEFAULT_IMAGE, help='DS 基础镜像（取原版 bundle）')
    ap.add_argument('--control', action='store_true', help='对照实验：跑 SQL 表单 Rr（应当 FAIL）')
    ap.add_argument('--keep', action='store_true', help='保留生成的 harness 文件')
    args = ap.parse_args()

    if not os.path.exists(args.bundle):
        raise SystemExit('!! 找不到 bundle: %s' % args.bundle)
    print('bundle : %s (%d bytes, md5=%s)' % (
        os.path.relpath(args.bundle, REPO), os.path.getsize(args.bundle),
        hashlib.md5(open(args.bundle, 'rb').read()).hexdigest()))

    ensure_stock(args.stock_dir, args.image)
    stock_bundle = os.path.join(args.stock_dir, 'detail.f1164795.js')
    bundle = args.bundle
    target = 'LDF'
    if args.control:
        bundle = stock_bundle
        target = 'Rr'
        print('control: 用原版 bundle 的 SQL 表单函数 %s 跑同一套断言（预期 FAILED）' % target)
    else:
        print('target : 归档补丁里的 LINEAGE_DAG 表单函数 LDF')
        # 先证明 oa 映射确实改成 LDF 了
        txt = open(args.bundle, encoding='utf-8').read()
        m = re.search(r'REMOTESHELL:\w+,([^}]*)\}', txt)
        print('oa 尾部映射: %s' % (m.group(1) if m else '(未匹配)'))

    js = build_harness(bundle, args.stock_dir, target, args.control)
    out_dir = os.path.join(HERE, 'build')
    os.makedirs(out_dir, exist_ok=True)
    js_path = os.path.join(out_dir, 'verify_ldf_form.%s.harness.js' % ('control' if args.control else 'ldf'))
    with open(js_path, 'w', encoding='utf-8') as fh:
        fh.write(js)
    print('harness: %s (%d 字符)' % (os.path.relpath(js_path, REPO), len(js)))
    print('-' * 78)
    expect_pass = not args.control
    rc, passed = run_harness(js_path, expect_pass=expect_pass)
    if not args.keep and os.path.exists(js_path):
        os.remove(js_path)
    if args.control:
        print('')
        print('== 对照实验结论：SQL 表单 Rr %s（预期 FAILED，用来证明断言不是空转）'
              % ('被断言拦住 -> 正确' if not passed else '竟然通过 -> 断言有问题!'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
