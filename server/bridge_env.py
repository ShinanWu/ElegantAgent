from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

from .paths import config_dir, is_frozen

logger = logging.getLogger(__name__)


def _ensure_executable(path: Path) -> None:
    if not path.is_file():
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _clear_quarantine(path: str) -> None:
    if sys.platform != "darwin" or not os.path.isfile(path):
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", path],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def bridge_state_root() -> Path:
    root = config_dir() / "bridge-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_bridge_launcher() -> str | None:
    override = os.environ.get("CURSOR_SDK_BRIDGE_BIN", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return str(path.resolve())

    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", ""))
        candidates = [
            base / "cursor_sdk" / "_vendor" / "bridge" / "bin" / "cursor-sdk-bridge",
            base / "Resources" / "cursor_sdk" / "_vendor" / "bridge" / "bin" / "cursor-sdk-bridge",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())

    try:
        from cursor_sdk._vendor import resolve_bridge_path

        return resolve_bridge_path()
    except Exception:
        return None


def prepare_bridge_env() -> str | None:
    launcher = resolve_bridge_launcher()
    if not launcher:
        logger.error("未找到 cursor-sdk-bridge 可执行文件")
        return None

    bridge_dir = Path(launcher).parent
    for name in ("cursor-sdk-bridge", "node"):
        binary = bridge_dir / name
        if binary.is_file():
            real = binary.resolve()
            _ensure_executable(real)
            _clear_quarantine(str(real))

    os.environ["CURSOR_SDK_BRIDGE_BIN"] = str(Path(launcher).resolve())
    logger.info("使用 bridge: %s", os.environ["CURSOR_SDK_BRIDGE_BIN"])
    return os.environ["CURSOR_SDK_BRIDGE_BIN"]


def workspace_path(preferred: str) -> str:
    path = Path(preferred or Path.home()).expanduser()
    if path.is_dir():
        return str(path.resolve())
    home = Path.home()
    logger.warning("工作目录无效 %s，回退到 %s", preferred, home)
    return str(home.resolve())
