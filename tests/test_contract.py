# -*- coding: utf-8 -*-
"""单元②（apps/lineage-api）对外 HTTP 契约的**漂移守卫**。

`contracts/openapi.yaml` 是这次「可独立部署」改造的产物，也是新平台
（`integrations/lineage-client`）、apps/web 前端、海豚插件三方唯一的耦合面。
契约由真实路由生成，所以最常见的坏情况是：**改了路由表，忘了重新生成契约**
—— 此时契约会安静地骗人（调用方按契约写代码，实际 404）。

这个文件就是为了让这种情况**立刻变红**，只测三件事：

1. **文件与路由同步**：`contracts/openapi.yaml` 与按路由渲染出来的文本逐字节一致；
2. **契约不缺端点**：路由表里的每个端点都在契约里，且方向（GET/POST）正确；
3. **没有绕开契约的分支**：`do_GET` / `do_POST` 里新出现的路径字面量必须已被契约声明
   （静态资源托管等内部路径走显式白名单，加白名单也会在 review 里被看见）。

跑法：``.venv/bin/python -m pytest tests/test_contract.py -q``
修法：``.venv/bin/python tools/gen_openapi.py``（重新生成契约，再看 diff 是否合理）
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
#: 与 pytest.ini 的 pythonpath 一致；也保证单独跑本文件时能导入内核
for _p in (REPO_ROOT / "apps" / "lineage-api", REPO_ROOT / "packages" / "lineage-core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from lineage.serve.api_server import DYNAMIC_GET_ROUTES  # noqa: E402

API_SERVER = REPO_ROOT / "apps" / "lineage-api" / "lineage" / "serve" / "api_server.py"
GEN_OPENAPI = REPO_ROOT / "tools" / "gen_openapi.py"
SPEC_FILE = REPO_ROOT / "contracts" / "openapi.yaml"

#: 不经契约声明的内部路径（静态托管 + 实现细节），加进来会在这里留下 review 痕迹
INTERNAL_PATH_ALLOWLIST = {
    "/app", "/app/",  # 静态托管：apps/web 单元契约由 tests/test_web_unit.py 守
    "/",              # 只是 parsed.path.rstrip("/") 的兜底值，实现里没有 "/" 的处理器
}
#: 前缀匹配的动态路由：路径字面量 → 契约里的动态路径
PREFIX_TO_DYNAMIC = {"/report/": "/report/<report_id>"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("gen_openapi_for_contract_test", GEN_OPENAPI)


def _parse_spec(text: str) -> tuple[set, set, dict]:
    """极简 openapi.yaml 解析（只认本文件的实际结构：2 空格路径 / 4 空格方法）。

    不引 PyYAML：契约格式是我们自己生成的，形状稳定；用 20 行换来"零新依赖"。
    """
    post, get, raw = set(), set(), {}
    current = None
    for line in text.splitlines():
        if line.startswith("  /") and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            raw[current] = set()
            continue
        m = re.match(r"^    (get|post):\s*$", line)
        if m and current:
            (post if m.group(1) == "post" else get).add(current)
            raw[current].add(m.group(1))
    return post, get, raw


def _dispatch_path_literals() -> set:
    """从 Handler.do_GET / do_POST 的源码里抠出所有以 "/" 开头的路径字面量。

    用 AST 而不是正则，避免把注释/文档字符串里的路径也算进来。
    """
    tree = ast.parse(API_SERVER.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name not in ("do_GET", "do_POST"):
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    val = sub.value
                    if val.startswith("/") and " " not in val:
                        found.add(val)
    return found


# --------------------------------------------------------------------------- #
# 1. 契约文件必须就是「按当前路由渲染出来的那份」
# --------------------------------------------------------------------------- #
def test_spec_file_is_in_sync_with_routes():
    rendered = gen.render_spec()
    on_disk = SPEC_FILE.read_text(encoding="utf-8")
    assert on_disk == rendered, (
        "contracts/openapi.yaml 与真实路由不一致（契约漂移）。\n"
        "修法：.venv/bin/python tools/gen_openapi.py  然后 review diff。\n"
        "提示：改了 apps/lineage-api 的路由表就要重新生成契约。"
    )


# --------------------------------------------------------------------------- #
# 2. 契约必须覆盖真实路由，且方向正确、且不臆造端点
# --------------------------------------------------------------------------- #
def test_spec_covers_every_implemented_route():
    post_paths, get_paths = gen.implemented_routes()
    spec_post, spec_get, _ = _parse_spec(SPEC_FILE.read_text(encoding="utf-8"))

    want_post = {gen.spec_path(p) for p in post_paths}
    want_get = {gen.spec_path(p) for p in get_paths}

    assert want_post - spec_post == set(), f"契约漏了 POST 端点: {sorted(want_post - spec_post)}"
    assert want_get - spec_get == set(), f"契约漏了 GET 端点: {sorted(want_get - spec_get)}"
    assert spec_post - want_post == set(), f"契约里有实现不存在的 POST 端点: {sorted(spec_post - want_post)}"
    assert spec_get - want_get == set(), f"契约里有实现不存在的 GET 端点: {sorted(spec_get - want_get)}"


def test_spec_uses_openapi_parameter_syntax():
    """动态路径必须是 {param} 写法，且带 required: true —— 否则调用方没法生成客户端。"""
    text = SPEC_FILE.read_text(encoding="utf-8")
    assert "/report/<" not in text, "契约里残留了实现侧写法 <report_id>，应为 {report_id}"
    assert "  /report/{report_id}:" in text
    block = text.split("  /report/{report_id}:", 1)[1].split("\n  /", 1)[0]
    assert "name: report_id" in block and "required: true" in block


# --------------------------------------------------------------------------- #
# 3. 没有绕开契约的分支：新写的路径必须先进契约（或显式白名单）
# --------------------------------------------------------------------------- #
def test_no_undeclared_paths_in_dispatch():
    post_paths, get_paths = gen.implemented_routes()
    all_impl = set(post_paths) | set(get_paths)
    declared = {gen.spec_path(p) for p in all_impl}
    declared |= all_impl  # 实现侧写法（如 /report/<report_id>）也算「已声明」
    declared |= INTERNAL_PATH_ALLOWLIST
    declared |= set(PREFIX_TO_DYNAMIC)  # 前缀匹配用的字面量（如 "/report/"）

    literals = _dispatch_path_literals()
    undeclared = sorted(p for p in literals if p not in declared)
    assert not undeclared, (
        "do_GET / do_POST 里出现了契约未声明的路径：%s\n"
        "要么把路由登记进 api_server 的 ROUTES/GET_ROUTES/DYNAMIC_GET_ROUTES（然后重新生成契约），"
        "要么说明它是内部路径并加进本文件的 INTERNAL_PATH_ALLOWLIST。" % undeclared
    )


def test_dynamic_get_route_is_reachable_by_prefix_match():
    """动态路由地址真的能被前缀匹配命中（契约声明的端点必须可达）。"""
    src = API_SERVER.read_text(encoding="utf-8")
    for dynamic in DYNAMIC_GET_ROUTES:
        prefix = dynamic.split("<", 1)[0]  # /report/<report_id> → /report/
        assert f'startswith("{prefix}")' in src, f"{dynamic} 声明了但没有前缀匹配实现: {prefix}"
