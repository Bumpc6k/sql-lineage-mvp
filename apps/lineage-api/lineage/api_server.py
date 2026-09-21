# -*- coding: utf-8 -*-
"""兼容别名（**不要在这里写实现**）。

实现已迁到 `lineage/serve/api_server.py`；这个模块只为保住仓库外的部署契约：
`/usr/local/bin/start-lineage-api.sh` 里用的是 `python -m lineage.api_server`。
"""
from lineage.serve.api_server import *          # noqa: F401,F403
from lineage.serve.api_server import main       # noqa: F401

if __name__ == "__main__":
    main()
