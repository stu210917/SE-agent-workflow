#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目入口（免安装即可运行）。

用法::

    python run.py run                      # 用内置示例需求跑完整流程
    python run.py run --spec "写一个 CSV 去重工具"
    python run.py graph --format mermaid   # 查看流程图
    python run.py agents                   # 查看 Agent 的读写声明
    python run.py rules                    # 查看修复规则清单

本项目复用了项目1 的代码审查 Agent，因此把仓库根目录加入 ``sys.path``，
让 ``codereview``（以 vendored 方式内置）与 ``workflow`` 都能被导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow.cli import main  # noqa: E402  (必须在 sys.path 调整之后导入)

if __name__ == "__main__":
    sys.exit(main())
