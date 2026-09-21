# -*- coding: utf-8 -*-
"""命令行入口包（由原 1452 行的 cli.py 拆成 5 族 + 公共工具）。

对外接口保持不变：``python -m lineage.cli <子命令>``、``from lineage import cli; cli.main([...])``。
下面用星号把各族自有名字重新挂回包上 —— 这些原来都是 cli 单文件的模块级名字，
测试与文档脚本会直接 ``cli.xxx`` 取用，属于对外接口，不能因为重构而消失。
"""
# 家族模块必须先 import：子命令注册（_SUBCOMMAND_RUNNERS[...] = cmd_x）是 import 副作用
from lineage.cli.common import *         # noqa: F401,F403
from lineage.cli.graph_cmds import *     # noqa: F401,F403
from lineage.cli.ds_cmds import *        # noqa: F401,F403
from lineage.cli.kb_cmds import *        # noqa: F401,F403
from lineage.cli.generate_cmds import *  # noqa: F401,F403
from lineage.cli.main import *           # noqa: F401,F403
from lineage.cli.main import main        # noqa: F401
