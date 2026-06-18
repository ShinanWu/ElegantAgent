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
from .paths import load_dotenv_if_present, resource_root
from .runtime import find_free_port, get_manager, manager_lifespan, start_manager, stop_manager
from .ws_hub import register_client, set_event_loop, shell_visible, unregister_client

load_dotenv_if_present()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PUBLIC = resource_root()
APP_CONFIG = load_config()
HOST = APP_CONFIG.host or os.environ.get("HOST", "127.0.0.1")
PORT = APP_CONFIG.port or int(os.environ.get("PORT", "3847"))


class SetupPayload(BaseModel):
    api_key: str = Field(min_length=10)
    default_cwd: str = ""
    default_model: str = "composer-2.5"


class SettingsPayload(BaseModel):
    api_key: str = ""
    default_cwd: str = ""
    default_model: str = "composer-2.5"


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
        url = f"http://{HOST}:{PORT}"
        logger.info("Cursor Agent π 运行于 %s", url)
        if os.environ.get("OPEN_BROWSER", "1") == "1" and not os.environ.get(
            "CURSOR_AGENT_NO_BROWSER"
        ):
            webbrowser.open(url)
        yield


app = FastAPI(title="Cursor Agent π", lifespan=lifespan)
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
        "ready": manager is not None,
        "url": f"http://{HOST}:{PORT}",
        "defaultCwd": APP_CONFIG.default_cwd or str(Path.home()),
        "defaultModel": APP_CONFIG.default_model,
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

    if payload.api_key.strip():
        APP_CONFIG.api_key = payload.api_key.strip()

    if payload.default_cwd.strip():
        APP_CONFIG.default_cwd = payload.default_cwd.strip()
    elif not APP_CONFIG.default_cwd:
        APP_CONFIG.default_cwd = str(Path.home())

    if payload.default_model.strip():
        APP_CONFIG.default_model = payload.default_model.strip() or "composer-2.5"

    save_config(APP_CONFIG)
    return {"ok": True, **APP_CONFIG.public_view()}


@app.post("/api/upload")
async def upload_file(
    agent_id: str = Form(...),
    file: UploadFile = File(...),
):
    manager = await ensure_manager()
    if manager is None:
        return {"ok": False, "detail": "请先完成设置"}
    record = manager.get_agent(agent_id)
    if record is None:
        return {"ok": False, "detail": "Agent 不存在"}
    data = await file.read()
    info = save_upload(record.cwd, file.filename or "file", data)
    return {"ok": True, **info}


async def ensure_manager() -> AgentManager | None:
    """已配置但尚未启动时懒加载 AgentManager（含 bridge）。"""
    if not APP_CONFIG.is_configured:
        return None
    manager = get_manager()
    if manager is not None:
        return manager
    try:
        return await start_manager(APP_CONFIG)
    except Exception:
        logger.exception("启动 AgentManager 失败")
        return None


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    register_client(ws)
    manager = await ensure_manager()
    send_lock = asyncio.Lock()

    async def emit(payload: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def run_send(agent_id: str, message: str, attachments: list | None, display_content: str | None) -> None:
        nonlocal manager
        manager = await ensure_manager()
        if manager is None:
            await emit({"type": "error", "message": "请先完成设置"})
            return
        try:
            await manager.send_message(
                agent_id, message, emit, attachments, display_content=display_content
            )
        except Exception:
            logger.exception("send_message failed")
            await emit({"type": "error", "message": "消息发送失败"})

    async def run_discussion_send(discussion_id: str, message: str) -> None:
        nonlocal manager
        manager = await ensure_manager()
        if manager is None:
            await emit({"type": "error", "message": "请先完成设置"})
            return
        try:
            await manager.discussions.send_message(discussion_id, message, emit)
        except Exception:
            logger.exception("discussion_send failed")
            await emit({"type": "error", "message": "讨论消息发送失败"})

    await emit(
        {
            "type": "hello",
            "needsSetup": not APP_CONFIG.is_configured,
            "defaultCwd": APP_CONFIG.default_cwd or str(Path.home()),
            "defaultModel": APP_CONFIG.default_model,
            "url": f"http://{HOST}:{PORT}",
            "shellVisible": shell_visible(),
        }
    )

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
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
                record = manager.get_agent(data["agentId"])
                if record:
                    await emit(
                        {
                            "type": "agent",
                            "agent": record.to_detail(running=record.id in manager._runs),
                        }
                    )

            elif msg_type == "create_agent":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": "请先完成设置"})
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
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
                try:
                    record = manager.update_agent(data["agentId"], data)
                    await emit(
                        {
                            "type": "agent_updated",
                            "agent": record.to_detail(running=record.id in manager._runs),
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
                if manager:
                    manager.delete_agent(data["agentId"])
                await emit({"type": "agent_deleted", "agentId": data["agentId"]})

            elif msg_type == "read_agent_files":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
                overrides = {
                    k: data[k]
                    for k in ("rulesDir", "skillsDir", "memoryDir")
                    if k in data
                }
                files = manager.read_agent_files(data["agentId"], overrides or None)
                await emit({"type": "agent_files", "agentId": data["agentId"], **files})

            elif msg_type == "read_agent_file":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
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

            elif msg_type == "write_agent_file":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
                manager.write_agent_file(
                    data["agentId"],
                    data.get("source", "rules"),
                    data["path"],
                    data["content"],
                )
                await emit(
                    {
                        "type": "agent_file_saved",
                        "agentId": data["agentId"],
                        "source": data.get("source", "rules"),
                        "path": data["path"],
                    }
                )

            elif msg_type == "list_models":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "models", "models": []})
                    continue
                models = await manager.list_models()
                await emit({"type": "models", "models": models})

            elif msg_type == "send":
                manager = await ensure_manager()
                if manager is None:
                    await emit({"type": "error", "message": "请先完成设置"})
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
                    await emit({"type": "error", "message": "请先完成设置"})
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
                    await emit({"type": "error", "message": "请先完成设置"})
                    continue
                asyncio.create_task(
                    run_discussion_send(data["discussionId"], data.get("message", ""))
                )

            else:
                await emit({"type": "error", "message": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("客户端断开连接")
    except Exception:
        logger.exception("WebSocket 错误")
    finally:
        unregister_client(ws)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="info")


if __name__ == "__main__":
    main()
