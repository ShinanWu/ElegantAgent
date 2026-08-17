#!/usr/bin/env python3
"""yoya 应用入口（开发 / PyInstaller 共用）。"""

from __future__ import annotations


def main() -> None:
    from server.paths import configure_logging

    configure_logging()
    from server.bridge_env import prepare_bridge_env

    prepare_bridge_env()
    from server.gui import run_gui

    run_gui()


if __name__ == "__main__":
    main()
