#!/usr/bin/env python3
"""验证对话持久化与 shell 事件驱动恢复逻辑。"""

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


def check_frontend_helpers() -> list[str]:
    failures: list[str] = []
    app_js = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    required = [
        "AgentFSM",
        "applyAgentEvent",
        "applyShellState",
        "onWindowShown",
        "paintActiveConversation",
        'case "shell"',
        "shellVisible",
        'applyAgentEvent(aid, "run_started"',
    ]
    for name in required:
        if name not in app_js:
            failures.append(f"app.js 缺少 {name}")
    if not (ROOT / "public" / "agent-fsm.js").is_file():
        failures.append("缺少 public/agent-fsm.js")
    fsm_js = (ROOT / "public" / "agent-fsm.js").read_text(encoding="utf-8") if (ROOT / "public" / "agent-fsm.js").is_file() else ""
    for token in ["Phase.IDLE", "Phase.RUNNING", "message_committed", "run_started", "snapshot"]:
        if token not in fsm_js:
            failures.append(f"agent-fsm.js 缺少 {token}")
    banned = [
        "__agentPiRefresh",
        "__agentPiSync",
        "state.threads",
        "state.runtimes",
        "refreshActiveView",
        "visibilitychange",
    ]
    for name in banned:
        if name in app_js:
            failures.append(f"app.js 仍包含已废弃的 {name}")
    shell_py = (ROOT / "server" / "macos_shell.py").read_text(encoding="utf-8")
    if "post_shell_visible" not in shell_py:
        failures.append("macos_shell 未发布 shell 事件")
    if "_notify_frontend_refresh" in shell_py:
        failures.append("macos_shell 仍包含 evaluate_js 刷新补丁")
    if not (ROOT / "server" / "ws_hub.py").is_file():
        failures.append("缺少 server/ws_hub.py")
    return failures


async def ws_conversation_roundtrip(host: str, port: int) -> list[str]:
    failures: list[str] = []
    import websockets

    uri = f"ws://{host}:{port}/ws"
    async with websockets.connect(uri, open_timeout=5) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "hello":
            failures.append(f"期望 hello，收到 {hello.get('type')}")
            return failures
        if hello.get("needsSetup"):
            failures.append("应用未配置 API Key，无法测试 get_agent")
            return failures
        if hello.get("shellVisible") is not True:
            failures.append(f"hello.shellVisible 应为 true，实际 {hello.get('shellVisible')}")

        await ws.send(json.dumps({"type": "list_agents"}))
        agents_msg = json.loads(await ws.recv())
        agents = agents_msg.get("agents") or []
        if not agents:
            failures.append("agents 列表为空")
            return failures

        target = max(agents, key=lambda a: a.get("messageCount") or 0)
        agent_id = target["id"]
        expected = target.get("messageCount") or 0

        await ws.send(json.dumps({"type": "get_agent", "agentId": agent_id}))
        detail_msg = json.loads(await ws.recv())
        if detail_msg.get("type") != "agent":
            failures.append(f"get_agent 返回 {detail_msg.get('type')} 而非 agent")
            return failures

        messages = detail_msg.get("agent", {}).get("messages") or []
        if len(messages) != expected:
            failures.append(
                f"Agent {agent_id} messageCount={expected} 但 messages 长度={len(messages)}"
            )
        elif expected == 0:
            failures.append("测试 Agent 没有历史消息，请先在应用中对话后再跑此脚本")
        else:
            print(f"    get_agent OK: {agent_id} -> {len(messages)} 条消息")

        from server.ws_hub import set_shell_visible

        await set_shell_visible(False)
        shell_msg = json.loads(await ws.recv())
        if shell_msg.get("type") != "shell" or shell_msg.get("visible") is not False:
            failures.append(f"shell hidden 事件异常: {shell_msg}")

        await set_shell_visible(True)
        shell_msg = json.loads(await ws.recv())
        if shell_msg.get("type") != "shell" or shell_msg.get("visible") is not True:
            failures.append(f"shell visible 事件异常: {shell_msg}")
        else:
            print("    shell 事件广播 OK")
    return failures


def start_test_server(host: str, port: int) -> threading.Thread:
    import uvicorn
    from server.app import app

    cfg = uvicorn.Config(app, host=host, port=port, reload=False, log_level="error")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, name="verify-conv", daemon=True)
    thread.start()
    return thread


def main() -> int:
    failures = check_frontend_helpers()
    if failures:
        return report(failures)

    print("==> 1. 事件驱动恢复 helper 检查通过")

    from server.config import load_config

    config = load_config()
    if not config.is_configured:
        print("==> 2. 跳过 WS（未配置 API Key）")
        return report([])

    host = "127.0.0.1"
    port = pick_port()
    base = f"http://{host}:{port}"
    print(f"==> 2. WS 对话 + shell 事件测试 @ {port}")

    start_test_server(host, port)
    for _ in range(100):
        try:
            http_get(f"{base}/api/status")
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    else:
        return report(["测试服务器未启动"])

    failures = asyncio.run(ws_conversation_roundtrip(host, port))
    return report(failures)


def report(failures: list[str]) -> int:
    print()
    if failures:
        print("❌ 验证失败:")
        for item in failures:
            print(f"   - {item}")
        return 1
    print("✅ 对话与 shell 事件验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
