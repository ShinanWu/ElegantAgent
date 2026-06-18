"""WebSocket 连接池：向所有已连接客户端广播应用级事件。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_clients: set[WebSocket] = set()
_shell_visible = True


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def register_client(ws: WebSocket) -> None:
    _clients.add(ws)


def unregister_client(ws: WebSocket) -> None:
    _clients.discard(ws)


def shell_visible() -> bool:
    return _shell_visible


async def broadcast(payload: dict[str, Any]) -> None:
    if not _clients:
        return
    text = json.dumps(payload, ensure_ascii=False)
    dead: list[WebSocket] = []
    for ws in list(_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        unregister_client(ws)


async def set_shell_visible(visible: bool) -> None:
    global _shell_visible
    if _shell_visible == visible:
        return
    _shell_visible = visible
    logger.info("shell visible=%s", visible)
    await broadcast({"type": "shell", "visible": visible})


def post_shell_visible(visible: bool) -> None:
    """从 GUI 主线程等非 async 上下文发布 shell 状态。"""
    if _loop is None:
        global _shell_visible
        _shell_visible = visible
        return
    asyncio.run_coroutine_threadsafe(set_shell_visible(visible), _loop)
