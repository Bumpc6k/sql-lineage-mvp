# -*- coding: utf-8 -*-
"""分层守卫：把「谁能 import 谁」变成测试用例（越界即失败）。

规则与白名单见 tools/check_layering.py 顶部注释；这里是它的入口包装。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "tools" / "check_layering.py"


def test_layering_no_violation() -> None:
    r = subprocess.run([sys.executable, str(CHECKER)], capture_output=True, text=True)
    assert r.returncode == 0, "分层越界：\n" + r.stdout + r.stderr
    assert "0 violations" in r.stdout
