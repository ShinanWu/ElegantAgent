"""PyInstaller 启动时准备 bridge 环境。"""
from __future__ import annotations

import sys

if getattr(sys, "frozen", False):
    try:
        from server.bridge_env import prepare_bridge_env

        prepare_bridge_env()
    except Exception:
        pass
