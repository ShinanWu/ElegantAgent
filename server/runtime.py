from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from .agent_manager import AgentManager
from .config import AppConfig, load_config, save_config

logger = logging.getLogger(__name__)

_manager: AgentManager | None = None
_manager_lock = asyncio.Lock()


def get_manager() -> AgentManager | None:
    return _manager


async def start_manager(config: AppConfig) -> AgentManager:
    global _manager
    async with _manager_lock:
        if _manager is not None:
            await _manager.stop()
        cwd = config.default_cwd or str(Path.home())
        _manager = AgentManager(config.api_key, cwd, config.default_model)
        await _manager.start()
        return _manager


async def stop_manager() -> None:
    global _manager
    async with _manager_lock:
        if _manager is not None:
            await _manager.stop()
            _manager = None


def find_free_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"无法在 {host} 上找到可用端口（从 {preferred} 起）")


@asynccontextmanager
async def manager_lifespan(config: AppConfig):
    # Bridge 在首次发消息时懒启动，避免应用启动阶段因引擎异常直接崩溃。
    try:
        yield
    finally:
        await stop_manager()
