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


async def get_or_create_manager(config: AppConfig) -> AgentManager:
    """从磁盘构造 AgentManager，不启动 bridge。"""
    global _manager
    async with _manager_lock:
        if _manager is None:
            cwd = config.default_cwd or str(Path.home())
            _manager = AgentManager(config.api_key, cwd, config.default_model)
        return _manager


async def start_manager(config: AppConfig) -> AgentManager:
    global _manager
    async with _manager_lock:
        if _manager is not None:
            await _manager.stop()
        cwd = config.default_cwd or str(Path.home())
        _manager = AgentManager(config.api_key, cwd, config.default_model)
        manager = _manager
    await manager.start()
    return manager


async def stop_manager() -> None:
    global _manager
    async with _manager_lock:
        if _manager is not None:
            await _manager.stop()
            _manager = None


async def boot_engine(config: AppConfig) -> None:
    """打开应用即在后台启动 bridge，不阻塞窗口出现。"""
    if not config.is_configured:
        return
    try:
        manager = await get_or_create_manager(config)
        await manager.start()
        logger.info("引擎已在应用启动时就绪")
        from .ws_hub import broadcast

        await broadcast({"type": "engine_status", "state": "ready"})
    except Exception:
        logger.exception("应用启动时引擎未能就绪")
        from .ws_hub import broadcast

        await broadcast(
            {
                "type": "engine_status",
                "state": "error",
                "message": "Agent 引擎未能启动，请检查 API Key 与网络。",
            }
        )


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
    try:
        yield
    finally:
        await stop_manager()
