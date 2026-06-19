"""原生窗口（pywebview）启动与单实例管理。"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .macos_shell import MacAppShell
from .shutdown import bind_uvicorn, shutdown_all

logger = logging.getLogger(__name__)

APP_TITLE = "尤雅"
_lock_fd: int | None = None
_shell = MacAppShell()


def _lock_path() -> Path:
    from .paths import config_dir

    return config_dir() / ".app.lock"


def acquire_instance_lock() -> bool:
    """获取单实例锁；False 表示已有实例在运行。"""
    global _lock_fd
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd
    return True


def release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


def show_main_window() -> None:
    """从 HTTP 或其他线程请求显示主窗口。"""
    _shell.show_main_window()


def prepare_shutdown() -> None:
    """退出前清理（Dock 右键退出 / Cmd+Q）。"""
    from .config import load_config

    config = load_config()
    host = config.host or "127.0.0.1"
    port = config.port or 3847
    shutdown_all(host=host, port=port)


def _server_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _server_alive(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"{_server_url(host, port)}/api/status",
            timeout=1.5,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _activate_existing_instance(host: str, port: int) -> bool:
    url = f"{_server_url(host, port)}/api/activate"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.exception("无法唤醒已有实例")
        return False


def _wait_for_server(host: str, port: int, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_alive(host, port):
            return
        time.sleep(0.15)
    raise RuntimeError(f"本地服务未在 {timeout:.0f}s 内启动（{host}:{port}）")


def _start_server_thread(host: str, port: int) -> threading.Thread:
    import uvicorn

    from .app import app

    config = uvicorn.Config(app, host=host, port=port, reload=False, log_level="info")
    server = uvicorn.Server(config)

    def _run() -> None:
        server.run()

    thread = threading.Thread(target=_run, name="uvicorn", daemon=False)
    thread.start()
    bind_uvicorn(server, thread)
    return thread


def _open_window(url: str) -> None:
    import webview

    from .desktop_api import DesktopApi

    if sys.platform == "darwin":
        _shell.install_delegate_hooks()

    api = DesktopApi()
    window = webview.create_window(
        APP_TITLE,
        url,
        width=1280,
        height=860,
        min_size=(960, 640),
        text_select=True,
        confirm_close=False,
        js_api=api,
    )
    _shell.attach_window(window, url=url)

    def _after_gui_start() -> None:
        from .desktop_api import enable_drop_path_capture

        enable_drop_path_capture()
        if sys.platform != "darwin":
            return

        def _on_main() -> None:
            _shell.finalize_hooks()

        try:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(_on_main)
        except Exception:
            _on_main()

    webview.start(_after_gui_start, debug=os.environ.get("CURSOR_AGENT_DEBUG") == "1")


def run_gui() -> None:
    """启动本地服务并打开原生窗口；关闭窗口后隐藏到后台，Cmd+Q 退出。"""
    try:
        import webview  # noqa: F401
    except ImportError:
        logger.warning("未安装 pywebview，回退到浏览器模式")
        from .app import main

        main()
        return

    from .bridge_env import bridge_state_root, prepare_bridge_env
    from .config import load_config
    from .shutdown import cleanup_bridge_processes

    prepare_bridge_env()
    cleanup_bridge_processes(bridge_state_root())

    config = load_config()
    host = config.host or "127.0.0.1"
    port = config.port or 3847
    url = _server_url(host, port)

    owns_server = acquire_instance_lock()
    if not owns_server:
        if _server_alive(host, port):
            logger.info("已有实例运行，唤醒窗口")
            if _activate_existing_instance(host, port):
                return
        try:
            _lock_path().unlink(missing_ok=True)
        except OSError:
            pass
        owns_server = acquire_instance_lock()
        if not owns_server and _server_alive(host, port):
            _activate_existing_instance(host, port)
            return

    os.environ.setdefault("CURSOR_AGENT_NO_BROWSER", "1")

    server_thread: threading.Thread | None = None
    if not _server_alive(host, port):
        server_thread = _start_server_thread(host, port)
        _wait_for_server(host, port)

    try:
        _open_window(url)
    finally:
        if owns_server:
            prepare_shutdown()
            release_instance_lock()

    logger.info("应用已退出")
