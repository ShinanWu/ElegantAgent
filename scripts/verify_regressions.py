#!/usr/bin/env python3
"""验证状态同步、设置热更新、持久化、安全边界与发布契约。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}" if detail else name)


def verify_frontend_snapshot() -> None:
    print("==> 1. 前端服务端快照为唯一真源")
    from server.bridge_env import resolve_bridge_launcher

    launcher = resolve_bridge_launcher()
    if not launcher:
        check("找到内置 Node", False, "未找到 cursor-sdk-bridge")
        return
    node = Path(launcher).parent / "node"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('public/agent-fsm.js', 'utf8') + '\n;globalThis.__fsm = AgentFSM;';
const ctx = { console };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const fsm = ctx.__fsm;
fsm.create('a', {
  running: true,
  messages: [{role:'user',content:'old'},{role:'assistant',content:'old reply'}],
  messageCount: 2,
});
const fx = fsm.dispatch('a', 'snapshot', {running:false, messages:[], messageCount:0});
console.log(JSON.stringify({
  phase: fx.agent.phase,
  messages: fx.agent.messages.length,
  messageCount: fx.agent.messageCount,
  messagesChanged: fx.messagesChanged,
  streamChanged: fx.streamChanged,
}));
"""
    proc = subprocess.run(
        [str(node), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        check("FSM 测试执行", False, proc.stderr.strip())
        return
    result = json.loads(proc.stdout)
    check("空快照清除旧消息", result["messages"] == 0)
    check("running=false 回到 idle", result["phase"] == "idle")
    check("清理运行期视图", result["streamChanged"] is True)


def verify_settings_restart() -> None:
    print("==> 2. 系统设置立即作用于 Manager")
    import server.app as app_mod
    from server.config import AppConfig

    old_config = app_mod.APP_CONFIG
    app_mod.APP_CONFIG = AppConfig(
        api_key="old-key-000000",
        default_cwd="/tmp",
        default_model="old-model",
    )
    stop = AsyncMock()
    start = AsyncMock()
    try:
        with patch.object(app_mod, "save_config"), patch.object(
            app_mod, "stop_manager", stop
        ), patch.object(app_mod, "start_manager", start):
            asyncio.run(
                app_mod.update_settings(
                    app_mod.SettingsPayload(
                        api_key="new-key-000000",
                        default_cwd="/tmp/new",
                        default_model="new-model",
                    )
                )
            )
        check("配置变化时停止旧 Manager", stop.await_count == 1)
        check("配置变化时启动新 Manager", start.await_count == 1)
        started_config = start.await_args.args[0]
        check(
            "新 Manager 使用新配置",
            started_config.api_key == "new-key-000000"
            and started_config.default_model == "new-model",
        )
    finally:
        app_mod.APP_CONFIG = old_config


def verify_late_websocket_events() -> None:
    print("==> 3. WebSocket 断线后丢弃迟到事件")
    from server.app import _SafeWebSocketEmitter

    class ClosedWebSocket:
        def __init__(self) -> None:
            self.calls = 0

        async def send_text(self, _: str) -> None:
            self.calls += 1
            raise RuntimeError("connection closed")

    ws = ClosedWebSocket()
    emitter = _SafeWebSocketEmitter(ws)  # type: ignore[arg-type]

    async def run() -> None:
        await emitter({"type": "models", "models": []})
        await emitter({"type": "models", "models": []})

    asyncio.run(run())
    check("首次发送失败后标记连接关闭", emitter.closed)
    check("后续事件不再调用已关闭连接", ws.calls == 1)


def verify_corrupt_storage() -> None:
    print("==> 4. 损坏持久化文件不阻断启动")
    from server.agents import load_agents
    from server.combined_summaries import load_combined_summaries
    from server.discussions import load_discussions

    with tempfile.TemporaryDirectory(prefix="yoya-storage-test-") as tmp:
        base = Path(tmp)
        cases = (
            ("agents", "server.agents.agents_file", load_agents),
            ("discussions", "server.discussions.discussions_file", load_discussions),
            (
                "combined_summaries",
                "server.combined_summaries.combined_summaries_file",
                load_combined_summaries,
            ),
        )
        for name, target, loader in cases:
            path = base / f"{name}.json"
            path.write_text("{broken", encoding="utf-8")
            with patch(target, return_value=path):
                try:
                    result = loader()
                except Exception as err:  # pragma: no cover - regression output
                    check(f"{name} 容错", False, type(err).__name__)
                else:
                    check(f"{name} 容错", result == {})


def verify_recovery_prompt() -> None:
    print("==> 5. 会话重建历史回填")
    from cursor_sdk import UserMessage
    from server.agent_manager import AgentManager
    from server.agents import AgentRecord

    manager = AgentManager.__new__(AgentManager)
    record = AgentRecord(
        id="a1",
        name="A",
        cwd="/tmp",
        model="test",
        messages=[
            {"role": "user", "content": "旧问题 [[πattach:/tmp/a.png]]"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "最新问题"},
        ],
    )
    current = UserMessage(text="当前完整 prompt")
    rebuilt = manager._rebuild_message_with_history(record, current)
    text = rebuilt.text
    check("包含之前的问答", "旧问题" in text and "旧回答" in text)
    check("不重复末尾最新消息", text.count("最新问题") == 0)
    check("附件内部标记不进入历史", "πattach" not in text)
    check("保留当前完整 prompt", "当前完整 prompt" in text)


def verify_single_instance_order() -> None:
    print("==> 6. 单实例先拿锁、后清理 bridge")
    source = (ROOT / "server" / "gui.py").read_text(encoding="utf-8")
    lock_at = source.index("owns_server = acquire_instance_lock()")
    cleanup_at = source.index("cleanup_bridge_processes(bridge_state_root())")
    check("bridge 清理发生在拿锁之后", cleanup_at > lock_at)
    check("不再删除仍可能被持有的锁文件", "_lock_path().unlink" not in source)


def verify_secret_migration() -> None:
    print("==> 7. API Key 迁移到系统钥匙串")
    from server import config as config_mod

    with tempfile.TemporaryDirectory(prefix="yoya-secret-test-") as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "api_key": "crsr_test_secret_123",
                    "default_cwd": "/tmp/project",
                    "host": "0.0.0.0",
                }
            ),
            encoding="utf-8",
        )
        with patch.object(config_mod, "config_file", return_value=path), patch.object(
            config_mod, "load_cursor_api_key", return_value=""
        ), patch.object(config_mod, "save_cursor_api_key", return_value=True):
            loaded = config_mod.load_config()
            persisted = json.loads(path.read_text(encoding="utf-8"))
            config_mod.save_config(loaded)
            saved = json.loads(path.read_text(encoding="utf-8"))

        check("旧密钥仍可用于当前会话", loaded.api_key == "crsr_test_secret_123")
        check("迁移后配置文件不含密钥", "api_key" not in persisted and "api_key" not in saved)
        check("本地服务强制使用回环地址", loaded.host == "127.0.0.1")
        check("配置文件权限限制为当前用户", (path.stat().st_mode & 0o777) == 0o600)


def verify_release_contract() -> None:
    print("==> 8. 版本、缓存、沙箱与包标识一致")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    spec = (ROOT / "packaging" / "yoya.spec").read_text(encoding="utf-8")
    distribution = (ROOT / "packaging" / "distribution.xml").read_text(encoding="utf-8")
    main_manager = (ROOT / "server" / "agent_manager.py").read_text(encoding="utf-8")
    discussion_manager = (ROOT / "server" / "discussion_manager.py").read_text(encoding="utf-8")
    index_template = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    gui = (ROOT / "server" / "gui.py").read_text(encoding="utf-8")

    from server import app as app_module

    index_response = asyncio.run(app_module.index())
    rendered_index = index_response.body.decode("utf-8")

    check("版本符合语义化格式", len(version.split(".")) == 3 and all(part.isdigit() for part in version.split(".")))
    check("应用包标识唯一", 'bundle_identifier="com.shinanwu.yoya"' in spec)
    check("安装包标识一致", "com.shinanwu.yoya" in distribution and "__VERSION__" in distribution)
    check(
        "入口页替换静态资源版本",
        "__YOYA_VERSION__" in index_template
        and "__YOYA_VERSION__" not in rendered_index
        and f"?v={version}" in rendered_index,
    )
    check(
        "入口页禁止 WebView 缓存",
        index_response.headers.get("cache-control")
        == "no-store, no-cache, must-revalidate, max-age=0",
    )
    check("原生窗口 URL 随版本变化", "_versioned_ui_url(host, port)" in gui)
    check("主 Agent 启用沙箱", "SandboxOptions(enabled=True)" in main_manager)
    check(
        "讨论 Agent 只开放读取工具",
        'tools=["read", "grep", "glob", "ls"]' in discussion_manager
        and "SandboxOptions(enabled=True)" in discussion_manager,
    )


def main() -> int:
    verify_frontend_snapshot()
    verify_settings_restart()
    verify_late_websocket_events()
    verify_corrupt_storage()
    verify_recovery_prompt()
    verify_single_instance_order()
    verify_secret_migration()
    verify_release_contract()
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败:")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print("✅ 回归验证全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
