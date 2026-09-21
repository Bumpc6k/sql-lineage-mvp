# -*- coding: utf-8 -*-
"""``python -m lineage.cli`` 入口（行为与原 cli.py 完全一致）。"""
import sys

from lineage.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
