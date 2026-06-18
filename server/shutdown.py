"""应用退出时的资源清理。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_uvicorn_server = None
_server_thread = None
_shutdown_done = False


def bind_uvicorn(server, thread) -> None:
    global _uvicorn_server, _server_thread
    _uvicorn_server = server
    _server_thread = thread


def stop_uvicorn(timeout: float = 5.0) -> None:
    global _uvicorn_server, _server_thread
    server = _uvicorn_server
    thread = _server_thread
    _uvicorn_server = None
    _server_thread = None

    if server is not None:
        server.should_exit = True
    if thread is not None and thread.is_alive():
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("uvicorn 线程未在 %.1fs 内结束", timeout)


def _request_api_shutdown(host: str, port: int, timeout: float = 5.0) -> bool:
    """在 uvicorn 事件循环内停止 AgentManager（避免跨 loop 调用 asyncio）。"""
    url = f"http://{host}:{port}/api/shutdown"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def cleanup_bridge_processes(state_root: Path | None = None) -> int:
    """清理可能残留的 cursor-sdk-bridge 子进程（仅限本应用 state 目录）。"""
    if sys.platform != "darwin":
        return 0

    from .bridge_env import bridge_state_root

    root_arg = str((state_root or bridge_state_root()).resolve())
    pids = list_bridge_pids(state_root)
    killed = 0

    for pid_int in pids:
        try:
            subprocess.run(["kill", str(pid_int)], check=False, capture_output=True)
        except OSError:
            continue

    if not pids:
        return 0

    import time

    time.sleep(0.4)
    for pid_int in list_bridge_pids(state_root):
        try:
            subprocess.run(["kill", "-9", str(pid_int)], check=False, capture_output=True)
            logger.info("已强制终止残留 bridge 进程 pid=%s", pid_int)
            killed += 1
        except OSError:
            pass

    return killed


def list_bridge_pids(state_root: Path | None = None) -> list[int]:
    """列出与本应用 state 目录相关的 bridge 进程 pid。"""
    if sys.platform != "darwin":
        return []

    from .bridge_env import bridge_state_root

    root_arg = str((state_root or bridge_state_root()).resolve())
    try:
        proc = subprocess.run(
            ["pgrep", "-f", root_arg],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []

    pids: list[int] = []
    for line in proc.stdout.splitlines():
        if line.strip().isdigit():
            pids.append(int(line.strip()))
    return [p for p in pids if p not in (0, os.getpid())]


def shutdown_all(
    host: str = "127.0.0.1",
    port: int = 3847,
    timeout: float = 12.0,
) -> None:
    """完整退出：API 停止 AgentManager → 停止 uvicorn → 清理 bridge 残留。"""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    from .bridge_env import bridge_state_root

    logger.info("正在关闭应用…")
    if _request_api_shutdown(host, port, timeout=min(timeout, 5.0)):
        logger.info("AgentManager 已通过 API 停止")
    stop_uvicorn(timeout=min(timeout, 5.0))
    killed = cleanup_bridge_processes(bridge_state_root())
    if killed:
        logger.info("额外清理了 %d 个 bridge 残留进程", killed)
    logger.info("应用资源已释放")
