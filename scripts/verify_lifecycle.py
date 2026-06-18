#!/usr/bin/env python3
"""验证 bridge 启动/退出与 API 生命周期（使用独立端口，避免与运行中实例冲突）。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_get(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode().strip()
        return json.loads(body) if body else {"ok": True}


def run_checks() -> int:
    from server.bridge_env import bridge_state_root, prepare_bridge_env
    from server.config import load_config
    from server.runtime import get_manager, start_manager, stop_manager
    from server.shutdown import (
        bind_uvicorn,
        cleanup_bridge_processes,
        list_bridge_pids,
        shutdown_all,
        stop_uvicorn,
    )

    failures: list[str] = []
    config = load_config()
    port = pick_port()
    host = "127.0.0.1"
    base = f"http://{host}:{port}"
    state_root = bridge_state_root()

    print(f"==> 使用独立测试端口 {port}")

    print("==> 1. bridge 启动与 stop_manager")
    prepare_bridge_env()
    before_orphans = list_bridge_pids(state_root)
    print(f"    测试前 state 相关 bridge 数: {len(before_orphans)}")

    if not config.is_configured:
        failures.append("未配置 API Key，无法测试 bridge 生命周期")
        return report(failures)

    async def bridge_cycle() -> None:
        pre = set(list_bridge_pids(state_root))
        await start_manager(config)
        manager = get_manager()
        assert manager is not None
        await manager.list_models()
        during = set(list_bridge_pids(state_root))
        new_pids = during - pre
        print(f"    本次新启动 bridge pid: {sorted(new_pids)}")
        if not new_pids:
            failures.append("start_manager 未启动新的 bridge 子进程")
        await stop_manager()
        await asyncio.sleep(1.0)
        remaining_new = new_pids & set(list_bridge_pids(state_root))
        print(f"    stop_manager 后仍存活的本实例 pid: {sorted(remaining_new)}")
        if remaining_new:
            failures.append(f"stop_manager 未能关闭本实例 bridge: {sorted(remaining_new)}")

    asyncio.run(bridge_cycle())

    print("==> 2. uvicorn + /api/shutdown + shutdown_all")
    import uvicorn
    from server.app import app

    cfg = uvicorn.Config(app, host=host, port=port, reload=False, log_level="error")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, name="verify-uvicorn", daemon=True)
    thread.start()
    bind_uvicorn(server, thread)

    for _ in range(100):
        try:
            http_get(f"{base}/api/status")
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    else:
        failures.append("测试 uvicorn 未启动")
        stop_uvicorn()
        return report(failures)

    pre = set(list_bridge_pids(state_root))

    async def warm_via_ws() -> None:
        import websockets

        uri = f"ws://{host}:{port}/ws"
        async with websockets.connect(uri, open_timeout=5) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "list_agents"}))
            await ws.recv()

    asyncio.run(warm_via_ws())
    during = set(list_bridge_pids(state_root))
    new_pids = during - pre
    print(f"    预热后新 bridge pid: {sorted(new_pids)}")

    try:
        http_post(f"{base}/api/shutdown")
        print("    POST /api/shutdown OK")
    except Exception as err:
        failures.append(f"/api/shutdown 失败: {err}")

    # 重置 shutdown 标志以便测试 shutdown_all
    import server.shutdown as shutdown_mod

    shutdown_mod._shutdown_done = False
    shutdown_all(host=host, port=port)
    time.sleep(0.8)

    if thread.is_alive():
        failures.append("shutdown_all 后 uvicorn 线程仍存活")

    leftover_new = new_pids & set(list_bridge_pids(state_root))
    print(f"    shutdown_all 后本实例 bridge 残留: {sorted(leftover_new)}")
    if leftover_new:
        failures.append(f"shutdown_all 后 bridge 残留: {sorted(leftover_new)}")

    print("==> 3. 前端 busy 逻辑")
    app_js = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    send_block = app_js.split("function sendMessage")[1].split("\nfunction ")[0]
    if "markSessionRunning" in send_block:
        failures.append("sendMessage 仍乐观设置 busy")
    else:
        print("    sendMessage 不提前禁用发送键 ✓")
    if "syncRuntimeFromServer" in app_js and "applyShellState" in app_js:
        print("    服务端 busy 同步 + shell 事件 ✓")
    else:
        failures.append("缺少 syncRuntimeFromServer 或 applyShellState")

    return report(failures)


def report(failures: list[str]) -> int:
    print()
    if failures:
        print("❌ 验证失败:")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✅ 全部验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_checks())
