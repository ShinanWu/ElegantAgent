from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent_manager import AgentManager
from .agent_workspace import save_upload
from .config import AppConfig, load_config, save_config
from .paths import configure_logging, load_dotenv_if_present, log_file, resource_root
from .runtime import (
    boot_engine,
    find_free_port,
    get_manager,
    get_or_create_manager,
    manager_lifespan,
    start_manager,
    stop_manager,
)
from .ws_hub import register_client, set_event_loop, shell_visible, unregister_client
from .version import app_version

load_dotenv_if_present()
configure_logging()
logger = logging.getLogger(__name__)

PUBLIC = resource_root()
APP_CONFIG = load_config()
HOST = "127.0.0.1"
PORT = APP_CONFIG.port or int(os.environ.get("PORT", "3847"))


class SetupPayload(BaseModel):
    api_key: str = Field(min_length=10)
    default_cwd: str = ""
    default_model: str = "composer-2.5"


class SettingsPayload(BaseModel):
    api_key: str = ""
    default_cwd: str = ""
    default_model: str = "composer-2.5"


class _SafeWebSocketEmitter:
    """连接关闭后静默丢弃后台任务的迟到事件。"""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.closed = False
        self._lock = asyncio.Lock()

    async def __call__(self, payload: dict) -> None:
        if self.closed:
            return
        async with self._lock:
            if self.closed:
                return
            try:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.closed = True
                unregister_client(self.ws)
                logger.debug("丢弃已断开 WebSocket 的迟到事件", exc_info=True)

    def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    global APP_CONFIG, PORT
    if not APP_CONFIG.is_configured:
        env_key = os.environ.get("CURSOR_API_KEY", "").strip()
        if env_key:
            APP_CONFIG.api_key = env_key
            APP_CONFIG.default_cwd = APP_CONFIG.default_cwd or str(Path.home())
            save_config(APP_CONFIG)

    if APP_CONFIG.port:
        PORT = APP_CONFIG.port
    else:
        PORT = find_free_port(HOST, PORT)
        APP_CONFIG.port = PORT
        save_config(APP_CONFIG)

    async with manager_lifespan(APP_CONFIG):
        set_event_loop(asyncio.get_running_loop())
        if APP_CONFIG.is_configured:
            asyncio.create_task(boot_engine(APP_CONFIG))
        url = f"http://{HOST}:{PORT}"
        logger.info("yoya 运行于 %s", url)
        if os.environ.get("OPEN_BROWSER", "1") == "1" and not os.environ.get(
            "CURSOR_AGENT_NO_BROWSER"
        ):
            webbrowser.open(url)
        yield


app = FastAPI(title="yoya", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PUBLIC), name="static")


@app.get("/")
async def index():
    return FileResponse(PUBLIC / "index.html")


@app.post("/api/activate")
async def activate():
    from .gui import show_main_window

    show_main_window()
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown():
    from .runtime import stop_manager

    await stop_manager()
    return {"ok": True}


@app.get("/api/status")
async def status():
    manager = get_manager()
    return {
        "configured": APP_CONFIG.is_configured,
        "ready": manager is not None and manager.is_started,
        "url": f"http://{HOST}:{PORT}",
        "defaultCwd": APP_CONFIG.default_cwd or str(Path.home()),
        "defaultModel": APP_CONFIG.default_model,
        "version": app_version(),
    }


@app.post("/api/setup")
async def setup(payload: SetupPayload):
    global APP_CONFIG
    await stop_manager()
    APP_CONFIG = AppConfig(
        api_key=payload.api_key.strip(),
        default_cwd=(payload.default_cwd or str(Path.home())).strip(),
        default_model=payload.default_model.strip() or "composer-2.5",
        host=HOST,
        port=PORT,
    )
    save_config(APP_CONFIG)
    await start_manager(APP_CONFIG)
    return {"ok": True, **APP_CONFIG.public_view()}


@app.post("/api/settings")
async def update_settings(payload: SettingsPayload):
    """更新系统设置；API Key 留空则保持不变。"""
    global APP_CONFIG
    if not APP_CONFIG.is_configured and not payload.api_key.strip():
        raise HTTPException(status_code=400, detail="请先填写 API Key")

    previous = (
        APP_CONFIG.api_key,
        APP_CONFIG.default_cwd,
        APP_CONFIG.default_model,
    )

    if payload.api_key.strip():
        APP_CONFIG.api_key = payload.api_key.strip()

    if payload.default_cwd.strip():
        APP_CONFIG.default_cwd = payload.default_cwd.strip()
    elif not APP_CONFIG.default_cwd:
        APP_CONFIG.default_cwd = str(Path.home())

    if payload.default_model.strip():
        APP_CONFIG.default_model = payload.default_model.strip() or "composer-2.5"

    save_config(APP_CONFIG)
    current = (
        APP_CONFIG.api_key,
        APP_CONFIG.default_cwd,
        APP_CONFIG.default_model,
    )
    if current != previous:
        await stop_manager()
        await start_manager(APP_CONFIG)
    return {"ok": True, **APP_CONFIG.public_view()}


@app.post("/api/upload")
async def upload_file(
    agent_id: str = Form(...),
    file: UploadFile = File(...),
):
    manager = await ensure_manager()
    if manager is None:
        return {"ok": False, "detail": manager_unavailable_message()}
    record = manager.get_agent(agent_id)
    if record is None:
        return {"ok": False, "detail": "Agent 不存在"}
    data = await file.read()
    info = save_upload(record.cwd, file.filename or "file", data)
    return {"ok": True, **info}


def manager_unavailable_message() -> str:
    if not APP_CONFIG.is_configured:
        return "请先完成设置"
    return (
        "Agent 引擎未能启动，请检查 API Key 与网络。"
        f"详细日志：{log_file()}"
    )


async def ensure_manager(*, start_bridge: bool = False) -> AgentManager | None:
    """已配置时加载 AgentManager。start_bridge=True 时等待引擎就绪。"""
    if not APP_CONFIG.is_configured:
        return None
    try:
        manager = await get_or_create_manager(APP_CONFIG)
    except Exception:
        logger.exception("创建 AgentManager 失败")
        return None
    if not start_bridge:
        return manager
    try:
        await manager.start()
        return manager
    except Exception:
        logger.exception("启动 AgentManager 失败")
        return None


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    register_client(ws)
    manager = get_manager()
    emit = _SafeWebSocketEmitter(ws)

    async def warmup_engine() -> None:
        nonlocal manager
        try:
            manager = await ensure_manager(start_bridge=True)
            if manager is None:
                await emit(
                    {
                        "type": "engine_status",
                        "state": "error",
                        "message": manager_unavailable_message(),
                    }
                )
                return
            await emit({"type": "engine_status", "state": "ready"})
        except Exception:
            logger.debug("warmup 状态未能送达客户端", exc_info=True)

    async def run_send(agent_id: str, message: str, attachments: list | None, display_content: str | None) -> None:
        nonlocal manager
        manager = await ensure_manager(start_bridge=True)
        if manager is None:
            await emit(
                {
                    "type": "error",
                    "agentId": agent_id,
                    "message": manager_unavailable_message(),
                }
            )
            return
        try:
            await manager.send_message(
                agent_id, message, emit, attachments, display_content=display_content
            )
        except Exception:
            logger.exception("send_message failed")
            await emit({"type": "error", "agentId": agent_id, "message": "消息发送失败"})

    async def run_discussion_send(discussion_id: str, message: str) -> None:
        nonlocal manager
        manager = await ensure_manager(start_bridge=True)
        if manager is None:
            await emit({"type": "error", "message": manager_unavailable_message()})
            return
        try:
            await manager.discussions.send_message(discussion_id, message, emit)
        except Exception:
            logger.exception("discussion_send failed")
            await emit(
                {
                    "type": "error",
                    "discussionId": discussion_id,
                    "scope": "discussion",
                    "message": "讨论消息发送失败",
                }
            )

    async def run_summarize_discussion(discussion_id: str, regenerate: bool) -> None:
        nonlocal manager
        manager = await ensure_manager(start_bridge=True)
        if manager is None:
            await emit({"type": "error", "message": manager_unavailable_message()})
            return
        try:
            await manager.discussions.summarize_discussion(
                discussion_id, emit, regenerate=regenerate
            )
        except Exception:
            logger.exception("summarize_discussion failed")
            await emit({"type": "error", "message": "讨论总结失败"})

    async def run_summarize_discussions(
        agent_id: str, discussion_ids: list, regenerate: bool
    ) -> None:
        nonlocal manager
        manager = await ensure_manager(start_bridge=True)
        if manager is None:
            await emit({"type": "error", "message": manager_unavailable_message()})
            return
        try:
            await manager.discussions.summarize_discussions(
                agent_id, discussion_ids, emit, regenerate=regenerate
            )
        except Exception:
            logger.exception("summarize_discussions failed")
            await emit({"type": "error", "message": "合并总结失败"})

    async def run_resummarize_combined(combined_id: str, regenerate_individuals: bool) -> None:
        nonlocal manager
        manager = await ensure_manager(start_bridge=True)
        if manager is None:
            await emit({"type": "error", "message": manager_unavailable_message()})
            return
        try:
            await manager.discussions.resummarize_combined(
                combined_id, emit, regenerate_individuals=regenerate_individuals
            )
        except Exception:
            logger.exception("resummarize_combined failed")
            await emit({"type": "error", "message": "重新合并总结失败"})

    async def run_list_models() -> None:
        nonlocal manager
        try:
            manager = await ensure_manager(start_bridge=True)
            if manager is None:
                await emit({"type": "models", "models": []})
                return
            models = await manager.list_models()
            await emit({"type": "models", "models": models})
        except Exception:
            logger.exception("list_models failed")
            await emit({"type": "models", "models": []})

    await emit(
        {
            "type": "hello",
            "needsSetup": not APP_CONFIG.is_configured,
            "defaultCwd": APP_CONFIG.default_cwd or str(Path.home()),
            "defaultModel": APP_CONFIG.default_model,
            "url": f"http://{HOST}:{PORT}",
            "shellVisible": shell_visible(),
            "engineReady": bool(get_manager() and get_manager().is_started),
            "version": app_version(),
        }
    )
    if APP_CONFIG.is_configured:
        asyncio.create_task(warmup_engine())

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "list_agents":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "agents", "agents": []})
                    continue
                await emit({"type": "agents", "agents": manager.list_agents()})

            elif msg_type == "get_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                record = manager.get_agent(data["agentId"])
                if record:
                    await emit(
                        {
                            "type": "agent",
                            "agent": record.to_detail(running=manager.is_agent_running(record.id)),
                        }
                    )

            elif msg_type == "create_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                record = manager.create_agent(
                    name=data.get("name"),
                    cwd=data.get("cwd"),
                    model=data.get("model"),
                )
                await emit(
                    {
                        "type": "agent_created",
                        "agent": record.to_detail(running=False),
                    }
                )

            elif msg_type == "update_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                try:
                    record = manager.update_agent(data["agentId"], data)
                    await emit(
                        {
                            "type": "agent_updated",
                            "agent": record.to_detail(running=manager.is_agent_running(record.id)),
                        }
                    )
                except ValueError as err:
                    await emit({"type": "error", "agentId": data.get("agentId"), "message": str(err)})
                except Exception as err:
                    logger.exception("update_agent failed")
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": f"保存失败: {err}",
                        }
                    )

            elif msg_type == "delete_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                manager.delete_agent(data["agentId"])
                await emit({"type": "agent_deleted", "agentId": data["agentId"]})

            elif msg_type == "reset_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                try:
                    record = await manager.reset_agent(data["agentId"])
                    await emit(
                        {
                            "type": "agent_reset",
                            "agent": record.to_detail(running=False),
                        }
                    )
                except ValueError as err:
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": str(err),
                        }
                    )
                except Exception as err:
                    logger.exception("reset_agent failed")
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": f"重置失败: {err}",
                        }
                    )

            elif msg_type == "read_agent_files":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                overrides = {
                    k: data[k]
                    for k in ("rulesDir", "skillsDir", "memoryDir")
                    if k in data
                }
                try:
                    files = manager.read_agent_files(data["agentId"], overrides or None)
                    await emit({"type": "agent_files", "agentId": data["agentId"], **files})
                except (KeyError, OSError, ValueError) as err:
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": f"读取 Agent 配置失败: {err}",
                        }
                    )

            elif msg_type == "read_agent_file":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                try:
                    content = manager.read_single_agent_file(
                        data["agentId"],
                        data.get("source", "rules"),
                        data["path"],
                    )
                    await emit(
                        {
                            "type": "agent_file",
                            "agentId": data["agentId"],
                            "source": data.get("source", "rules"),
                            "path": data["path"],
                            "content": content,
                        }
                    )
                except (KeyError, FileNotFoundError, ValueError, OSError) as err:
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": f"读取配置文件失败: {err}",
                        }
                    )

            elif msg_type == "write_agent_file":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                try:
                    manager.write_agent_file(
                        data["agentId"],
                        data.get("source", "rules"),
                        data["path"],
                        data.get("content", ""),
                    )
                    files = manager.read_agent_files(data["agentId"])
                    await emit(
                        {
                            "type": "agent_file_saved",
                            "agentId": data["agentId"],
                            "source": data.get("source", "rules"),
                            "path": data["path"],
                            **files,
                        }
                    )
                except (KeyError, ValueError, OSError) as err:
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": f"保存配置文件失败: {err}",
                        }
                    )

            elif msg_type == "list_models":
                asyncio.create_task(run_list_models())

            elif msg_type == "send":
                manager = await ensure_manager()
                if manager is None:
                    await emit(
                        {
                            "type": "error",
                            "agentId": data.get("agentId"),
                            "message": manager_unavailable_message(),
                        }
                    )
                    continue
                asyncio.create_task(
                    run_send(
                        data["agentId"],
                        data.get("message", ""),
                        data.get("attachments"),
                        data.get("content"),
                    )
                )

            elif msg_type == "cancel":
                manager = await ensure_manager()
                if manager:
                    await manager.cancel(data["agentId"], emit)

            elif msg_type == "list_discussions":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "discussions", "discussions": []})
                    continue
                discussions = manager.discussions.list_for_agent(data["agentId"])
                await emit(
                    {
                        "type": "discussions",
                        "agentId": data["agentId"],
                        "discussions": discussions,
                    }
                )

            elif msg_type == "create_discussion":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                discussion = manager.discussions.create(data["agentId"], data.get("anchor", {}))
                await emit(
                    {
                        "type": "discussion_created",
                        "discussion": discussion.to_dict(),
                    }
                )

            elif msg_type == "discussion_send":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                asyncio.create_task(
                    run_discussion_send(data["discussionId"], data.get("message", ""))
                )

            elif msg_type == "delete_discussion":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                deleted = manager.discussions.delete(data["discussionId"])
                if deleted is None:
                    await emit(
                        {
                            "type": "error",
                            "discussionId": data.get("discussionId"),
                            "message": "讨论不存在",
                        }
                    )
                else:
                    await emit(
                        {
                            "type": "discussion_deleted",
                            "discussionId": deleted.id,
                            "agentId": deleted.agent_id,
                        }
                    )

            elif msg_type == "set_discussion_collapsed":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                updated = manager.discussions.set_collapsed(
                    data["discussionId"], bool(data.get("collapsed"))
                )
                if updated is None:
                    await emit(
                        {
                            "type": "error",
                            "discussionId": data.get("discussionId"),
                            "scope": "discussion",
                            "message": "讨论不存在",
                        }
                    )

            elif msg_type == "summarize_discussion":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                asyncio.create_task(
                    run_summarize_discussion(
                        data["discussionId"],
                        bool(data.get("regenerate")),
                    )
                )

            elif msg_type == "update_discussion_summary":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                updated = manager.discussions.update_discussion_summary(
                    data["discussionId"], data.get("summary", "")
                )
                if updated is None:
                    await emit(
                        {
                            "type": "error",
                            "discussionId": data.get("discussionId"),
                            "message": "讨论不存在",
                        }
                    )
                else:
                    await emit(
                        {
                            "type": "discussion_summary_updated",
                            "discussion": updated.to_dict(),
                        }
                    )

            elif msg_type == "summarize_discussions":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                asyncio.create_task(
                    run_summarize_discussions(
                        data["agentId"],
                        data.get("discussionIds") or [],
                        bool(data.get("regenerate")),
                    )
                )

            elif msg_type == "list_combined_summaries":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "combined_summaries", "combinedSummaries": []})
                    continue
                items = manager.discussions.list_combined_for_agent(data["agentId"])
                await emit(
                    {
                        "type": "combined_summaries",
                        "agentId": data["agentId"],
                        "combinedSummaries": items,
                    }
                )

            elif msg_type == "update_combined_summary":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                updated = manager.discussions.update_combined_summary(
                    data["combinedSummaryId"], data.get("summary", "")
                )
                if updated is None:
                    await emit(
                        {
                            "type": "error",
                            "combinedSummaryId": data.get("combinedSummaryId"),
                            "message": "合并总结不存在",
                        }
                    )
                else:
                    await emit(
                        {
                            "type": "combined_summary_updated",
                            "combinedSummary": updated.to_dict(),
                        }
                    )

            elif msg_type == "resummarize_combined":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                asyncio.create_task(
                    run_resummarize_combined(
                        data["combinedSummaryId"],
                        bool(data.get("regenerateIndividuals")),
                    )
                )

            elif msg_type == "delete_combined_summary":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": manager_unavailable_message()})
                    continue
                deleted = manager.discussions.delete_combined_summary(
                    data["combinedSummaryId"]
                )
                if deleted is None:
                    await emit(
                        {
                            "type": "error",
                            "combinedSummaryId": data.get("combinedSummaryId"),
                            "message": "合并总结不存在",
                        }
                    )
                else:
                    await emit(
                        {
                            "type": "combined_summary_deleted",
                            "combinedSummaryId": deleted.id,
                            "agentId": deleted.agent_id,
                        }
                    )

            else:
                await emit({"type": "error", "message": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("客户端断开连接")
    except Exception:
        logger.exception("WebSocket 错误")
    finally:
        emit.close()
        unregister_client(ws)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="info")


if __name__ == "__main__":
    main()
