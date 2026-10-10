"""测试基座：让 backend_ingestion 的 `app` 包与 alert_engine 的同名包共存。

问题（本仓结构性的，不是本单引入的）：
两个模块都有顶层包 `app` —— `backend_ingestion/app` 与 `alert_engine/app`。
从仓库根跑一次 `pytest` 时，先导入的会永久占住 `sys.modules["app"]`，
另一个的 `import app.xxx` 全挂（实测 `ModuleNotFoundError: No module named 'app.rules'`
与 `'app.db' is not a package`）。加本文件之前"从根跑能过"，
只是因为当时**只有** alert_engine 有 `app` 包。

解法：本目录的 `app` 包以**本目录为根**重新注册，覆盖 `sys.modules["app"]`；
同时把 `backend_ingestion` 插到 `sys.path[0]`，让包内部的 `from app.xxx import yyy`
也解析到这里。实测这样 `pytest backend_ingestion/tests/` 与 `pytest alert_engine/tests/`
各自都能全绿。

⚠️ 已知限制：一次 `pytest` 进程里**同时**收集两个目录仍会互相踩
（`sys.modules["app"]` 只有一个位置）。根目录 `pytest.ini` 的 `testpaths`
已把两套用例分开：默认只跑仓库根 `tests/` 与 `alert_engine/tests/`，
backend 的用例用 `pytest backend_ingestion/tests/` 单独跑（见 README）。
把 top-level 包改名是根治办法，但那是跨模块重构，不在订单 15 范围内。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# settings.jwt_secret 是必填项（不设默认值），必须在 import app.* 之前注入，
# 否则 Settings() 构造即抛 ValidationError。
os.environ.setdefault("JWT_SECRET", "test-only-secret-0123456789abcdef")

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])

# 丢掉可能被 alert_engine 侧占用的顶层 `app`，让下一次 import 重新解析到本目录。
for _name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_name]

while BACKEND_ROOT in sys.path:
    sys.path.remove(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)
