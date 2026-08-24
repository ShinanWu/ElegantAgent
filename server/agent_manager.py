from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from cursor_sdk import (
    AgentOptions,
    AsyncClient,
    AsyncCursor,
    CursorAgentError,
    LocalAgentOptions,
    SendOptions,
    UserMessage,
)
from cursor_sdk.errors import UnsupportedRunOperationError
from cursor_sdk.types import SDKImage, SandboxOptions

from .agent_workspace import (
    clear_artifacts,
    list_agent_config,
    read_config_file,
    scaffold_agent_dir,
    write_config_file,
    write_soul,
)
from .agents import AgentRecord, load_agents, new_agent, save_agents
from .bridge_env import bridge_state_root, prepare_bridge_env, workspace_path
from .discussion_manager import DiscussionManager
from .prompt_builder import ATTACH_MARKER_RE, build_prompt_text, merge_user_display_content
from .stream_format import (
    append_text_segment,
    apply_payload_to_segments,
    finalize_segments,
    segments_content,
    segments_legacy_blocks,
    serialize_run_event,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
T = TypeVar("T")

IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}

_TERMINAL_RUN_STATUSES = frozenset(
    {"cancelled", "canceled", "completed", "failed", "error", "done", "success"}
)


async def _safe_cancel_run(run: Any, *, agent_id: str = "") -> None:
    if not run.supports("cancel"):
        return
    status = str(getattr(run, "status", "") or "").lower()
    if status in _TERMINAL_RUN_STATUSES:
        return
    try:
        await run.cancel()
    except UnsupportedRunOperationError:
        logger.debug(
            "Run already terminal, skip cancel (agent=%s run=%s)",
            agent_id or "?",
            getattr(run, "id", "?"),
        )
    except Exception:
        logger.exception("cancel run failed (agent=%s)", agent_id or "?")


class _AgentSilentError(CursorAgentError):
    """远端 agent 会话失效：send 完成但模型无任何输出。视为 stale agent 触发重建重试。"""

    def __init__(self) -> None:
        super().__init__(
            "agent session produced no output (likely expired)",
            code="agent_silent",
            is_retryable=True,
        )


class AgentManager:
    def __init__(self, api_key: str, default_cwd: str, default_model: str) -> None:
        self.api_key = api_key
        self.default_cwd = workspace_path(default_cwd)
        self.default_model = default_model
        self.agents = load_agents()
        self.discussions = DiscussionManager(self)
        self._client: AsyncClient | None = None
        self._sdk_agents: dict[str, Any] = {}
        self._runs: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancel_requested: dict[str, bool] = {}
        self._rebuilt_agents: set[str] = set()
        self._start_lock = asyncio.Lock()
        self._pending_runs: set[str] = set()

    @property
    def is_started(self) -> bool:
        return self._client is not None

    def is_agent_running(self, agent_id: str) -> bool:
        return agent_id in self._runs or agent_id in self._pending_runs

    async def start(self) -> None:
        async with self._start_lock:
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        if self._client is not None:
            return
        prepare_bridge_env()
        self._client = await AsyncClient.launch_bridge(
            workspace=self.default_cwd,
            state_root=str(bridge_state_root()),
            timeout=60,
        )
        logger.info("cursor-sdk-bridge 已连接")

    async def stop(self) -> None:
        await self.discussions.stop_all()
        for agent_id in list(self._runs.keys()):
            self._cancel_requested[agent_id] = True
            run = self._runs.get(agent_id)
            if run is not None:
                try:
                    await asyncio.wait_for(self._abort_run(agent_id, run), timeout=4.0)
                except asyncio.TimeoutError:
                    logger.warning("取消 Agent %s 的运行超时", agent_id)
                    self._runs.pop(agent_id, None)
        self._runs.clear()
        self._cancel_requested.clear()
        self._pending_runs.clear()

        for agent in list(self._sdk_agents.values()):
            try:
                await asyncio.wait_for(agent.close(), timeout=4.0)
            except asyncio.TimeoutError:
                logger.warning("关闭 SDK agent 超时")
            except Exception:
                logger.exception("failed to close agent")
        self._sdk_agents.clear()
        async with self._start_lock:
            await self._stop_client()

    async def _stop_client(self) -> None:
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                await asyncio.wait_for(client.aclose(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("关闭 bridge 客户端超时")
            except Exception:
                logger.exception("failed to close bridge client")

    async def _restart_bridge(self) -> None:
        logger.warning("正在重启 cursor-sdk-bridge …")
        self._runs.clear()
        await self.discussions.stop_all()
        for agent in list(self._sdk_agents.values()):
            try:
                await agent.close()
            except Exception:
                pass
        self._sdk_agents.clear()
        for record in self.agents.values():
            record.sdk_agent_id = None
        save_agents(self.agents)
        async with self._start_lock:
            await self._stop_client()
            await self._start_unlocked()

    @staticmethod
    def _is_recoverable_error(err: BaseException) -> bool:
        return AgentManager._is_bridge_error(err) or AgentManager._is_stale_agent_error(err)

    @staticmethod
    def _error_detail(err: BaseException) -> str:
        if isinstance(err, CursorAgentError):
            parts = [p for p in (err.code, err.message) if p]
            if parts:
                return ": ".join(parts)
        return str(err).strip()

    @staticmethod
    def _format_run_error(err: BaseException | None) -> str:
        if err is None:
            return "Agent 运行失败，请稍后重试。"
        if isinstance(err, _AgentSilentError):
            return (
                "暂时没能接上对话。请再发一次消息；"
                "若仍无回复，可检查网络或 API Key。"
            )
        detail = AgentManager._error_detail(err) or "Agent 运行失败，请稍后重试。"
        if AgentManager._is_bridge_error(err):
            return (
                "Agent 引擎连接已断开，自动恢复未成功。"
                "请再试一次；若仍失败，请完全退出应用后重新打开。"
                f"（{detail}）"
            )
        return detail

    @staticmethod
    def _is_bridge_error(err: BaseException) -> bool:
        text = str(err).lower()
        patterns = (
            "connecterror",
            "connection attempts failed",
            "bridge request failed",
            "connection refused",
            "connect call failed",
            "connection reset",
            "broken pipe",
            "timed out",
            "timeout",
            "eof",
            "closed",
        )
        return any(p in text for p in patterns)

    async def _bridge_call(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            return await fn()
        except CursorAgentError as err:
            if self._is_bridge_error(err):
                await self._restart_bridge()
                return await fn()
            raise
        except Exception as err:
            if self._is_bridge_error(err):
                await self._restart_bridge()
                return await fn()
            raise

    def list_agents(self) -> list[dict[str, Any]]:
        items = sorted(
            self.agents.values(),
            key=lambda a: a.updated_at,
            reverse=True,
        )
        return [a.to_summary(running=self.is_agent_running(a.id)) for a in items]

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def create_agent(
        self,
        name: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
    ) -> AgentRecord:
        resolved_cwd = workspace_path(cwd or self.default_cwd)
        scaffold_agent_dir(resolved_cwd)
        record = new_agent(
            name=name or "新 Agent",
            cwd=resolved_cwd,
            model=model or self.default_model,
        )
        self.agents[record.id] = record
        save_agents(self.agents)
        return record

    def update_agent(self, agent_id: str, patch: dict[str, Any]) -> AgentRecord:
        record = self.agents.get(agent_id)
        if record is None:
            raise ValueError("Agent 不存在")

        clean = {
            k: v
            for k, v in patch.items()
            if k not in ("type", "agentId", "agent_id")
        }
        reset_sdk = False

        if "name" in clean and clean["name"]:
            record.name = str(clean["name"]).strip()
        if "cwd" in clean and clean["cwd"]:
            new_cwd = workspace_path(str(clean["cwd"]))
            if new_cwd != record.cwd:
                record.cwd = new_cwd
                scaffold_agent_dir(new_cwd)
                reset_sdk = True
        if "model" in clean and clean["model"]:
            if clean["model"] != record.model:
                record.model = str(clean["model"])
                reset_sdk = True
        for key, attr in (
            ("enableSoul", "enable_soul"),
            ("enableRules", "enable_rules"),
            ("enableSkills", "enable_skills"),
            ("enableMemory", "enable_memory"),
        ):
            if key in clean:
                new_val = bool(clean[key])
                if getattr(record, attr) != new_val:
                    setattr(record, attr, new_val)
                    reset_sdk = True
        for key, attr in (
            ("rulesDir", "rules_dir"),
            ("skillsDir", "skills_dir"),
            ("memoryDir", "memory_dir"),
        ):
            if key in clean:
                new_val = str(clean[key] or "").strip()
                if getattr(record, attr) != new_val:
                    setattr(record, attr, new_val)
                    reset_sdk = True
        if "soul" in clean:
            write_soul(record.cwd, str(clean["soul"]))
            reset_sdk = True

        if reset_sdk:
            self._reset_sdk_binding(record)

        record.touch()
        save_agents(self.agents)
        return record

    def delete_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)
        save_agents(self.agents)
        sdk_agent = self._sdk_agents.pop(agent_id, None)
        if sdk_agent is not None:
            asyncio.create_task(sdk_agent.close())
        self.discussions.delete_for_agent(agent_id)

    async def reset_agent(self, agent_id: str) -> AgentRecord:
        record = self.agents.get(agent_id)
        if record is None:
            raise ValueError("Agent 不存在")

        self._cancel_requested[agent_id] = True
        run = self._runs.get(agent_id)
        if run is not None:
            try:
                await self._abort_run(agent_id, run)
            except Exception:
                logger.exception("重置 Agent %s 时取消运行失败", agent_id)
        self._cancel_requested.pop(agent_id, None)

        self._reset_sdk_binding(record)
        record.messages.clear()
        clear_artifacts(record.cwd)
        self.discussions.delete_for_agent(agent_id)
        record.touch()
        save_agents(self.agents)
        logger.info("已重置 Agent %s", agent_id)
        return record

    def read_agent_files(self, agent_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.agents[agent_id]
        return list_agent_config(record, overrides)

    def write_agent_file(
        self,
        agent_id: str,
        source: str,
        rel_path: str,
        content: str,
    ) -> None:
        record = self.agents[agent_id]
        write_config_file(record, source, rel_path, content)
        if source in ("soul", "rules", "skills", "memory"):
            self._reset_sdk_binding(record)
            record.touch()
            save_agents(self.agents)

    def read_single_agent_file(self, agent_id: str, source: str, rel_path: str) -> str:
        record = self.agents[agent_id]
        return read_config_file(record, source, rel_path)

    def _lock_for(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    async def _ensure_client(self) -> AsyncClient:
        await self.start()
        assert self._client is not None
        return self._client

    @staticmethod
    def _is_stale_agent_error(err: BaseException) -> bool:
        text = str(err).lower()
        patterns = (
            "internal error",
            "internal_error",
            "internal_server_error",
            "agent_not_found",
            "agent not found",
            "not_found",
            "session_not_found",
            "invalid agent",
        )
        if isinstance(err, CursorAgentError):
            code = (err.code or "").lower()
            if code in ("internal_error", "internal", "not_found", "failed_precondition", "agent_silent"):
                return True
        return any(p in text for p in patterns)

    def _reset_sdk_binding(self, record: AgentRecord) -> None:
        agent_id = record.id
        sdk_agent = self._sdk_agents.pop(agent_id, None)
        if sdk_agent is not None:
            asyncio.create_task(sdk_agent.close())
        record.sdk_agent_id = None
        save_agents(self.agents)
        logger.warning("已重置 Agent %s 的 SDK 绑定", agent_id)

    def _has_prior_conversation(self, record: AgentRecord) -> bool:
        """本地是否已有可回填的上文（不含本轮刚 commit 的最后一条 user）。"""
        history = record.messages[:-1] if record.messages else []
        for m in history:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = str(m.get("content") or "").strip()
            if content:
                return True
            segments = m.get("segments") or []
            if any(
                isinstance(seg, dict) and seg.get("type") == "text" and str(seg.get("text") or "").strip()
                for seg in segments
            ):
                return True
        return False

    def _mark_needs_history(self, record: AgentRecord, *, reason: str) -> None:
        if not self._has_prior_conversation(record):
            return
        self._rebuilt_agents.add(record.id)
        logger.info("将回填本地对话历史 agent=%s reason=%s", record.id, reason)

    async def _ensure_sdk_agent(self, record: AgentRecord) -> Any:
        """拿到可用的 SDK Agent：内存命中 → resume → 新建。

        新建或 resume 失败后重建时，若本地已有对话，标记回填历史，
        让用户感觉永远能接上，而不是丢上下文。
        """
        if record.id in self._sdk_agents:
            return self._sdk_agents[record.id]

        client = await self._ensure_client()
        options = AgentOptions(
            api_key=self.api_key,
            model=record.model,
            local=LocalAgentOptions(
                cwd=workspace_path(record.cwd),
                sandbox_options=SandboxOptions(enabled=True),
            ),
        )

        agent = None
        resumed = False
        previous_sdk_id = record.sdk_agent_id
        if previous_sdk_id:
            try:
                agent = await client.agents.resume(previous_sdk_id, options)
                resumed = True
            except Exception as err:
                if self._is_bridge_error(err):
                    # bridge 挂了：交给外层重启，不要当成「会话丢了」去新建空 Agent
                    raise
                logger.warning(
                    "恢复 SDK Agent %s 失败，将新建并回填历史: %s",
                    previous_sdk_id,
                    err,
                )
                self._reset_sdk_binding(record)

        if agent is None:
            agent = await client.agents.create(options)
            reason = "resume_failed" if previous_sdk_id else "no_sdk_id"
            self._mark_needs_history(record, reason=reason)

        record.sdk_agent_id = agent.agent_id
        save_agents(self.agents)
        self._sdk_agents[record.id] = agent
        if resumed:
            logger.info("已 resume SDK Agent %s → %s", record.id, agent.agent_id)
        return agent

    async def list_models(self) -> list[dict[str, str]]:
        client = await self._ensure_client()

        async def _list() -> list[dict[str, str]]:
            models = await AsyncCursor.models.list(
                client=client,
                api_key=self.api_key,
            )
            return [{"id": m.id, "name": getattr(m, "name", m.id)} for m in models]

        return await self._bridge_call(_list)

    def _resolve_attachment_path(self, record: AgentRecord, item: dict[str, Any]) -> Path | None:
        from .agent_workspace import resolve_attachment_path

        return resolve_attachment_path(record, item)

    def _build_user_message(
        self,
        record: AgentRecord,
        text: str,
        attachments: list[dict[str, Any]] | None,
    ) -> UserMessage:
        prompt = build_prompt_text(record, text)
        images: list[SDKImage] = []
        extra_lines: list[str] = []

        for item in attachments or []:
            path = self._resolve_attachment_path(record, item)
            if path is None:
                continue
            if path.is_dir():
                extra_lines.append(f"[引用目录: {path}]")
                continue
            mime, _ = mimetypes.guess_type(path.name)
            mime = mime or "application/octet-stream"
            if mime in IMAGE_MIME:
                logger.info("附加图片 %s mime=%s size=%s", path, mime, path.stat().st_size)
                images.append(SDKImage.from_file(path, mime_type=mime))
            else:
                extra_lines.append(f"[引用文件: {path}]")

        if extra_lines:
            prompt = prompt + "\n\n" + "\n".join(extra_lines)

        return UserMessage(text=prompt, images=images or None)

    def _rebuild_message_with_history(
        self,
        record: AgentRecord,
        current: UserMessage | str,
    ) -> UserMessage | str:
        """重建 agent 后，把本地历史对话回填进当前消息，让新 agent 读到上文。

        record.messages 末尾是本次刚 commit 的 user 消息，前面是历史。
        只回填文本对话，工具调用/文件改动等内部状态无法重建，但对话语境可续。
        """
        history = record.messages[:-1] if record.messages else []
        turns: list[str] = []
        for m in history:
            role = m.get("role")
            content = str(m.get("content") or "").strip()
            if not content:
                segments = m.get("segments") or []
                content = " ".join(
                    str(seg.get("text") or "")
                    for seg in segments
                    if isinstance(seg, dict) and seg.get("type") == "text"
                ).strip()
            if not content:
                continue
            label = "用户" if role == "user" else "助手" if role == "assistant" else None
            if label is None:
                continue
            # 去掉附件标记，只保留可读文本，避免污染重建后的上下文
            cleaned = ATTACH_MARKER_RE.sub("", content).strip()
            content = cleaned or "(含附件)"
            if len(content) > 4000:
                content = content[:3999] + "…"
            turns.append(f"{label}: {content}")
        if not turns:
            return current

        # 只保留最近若干轮，避免提示过长
        if len(turns) > 40:
            turns = turns[-40:]

        history_text = "\n\n".join(turns)
        current_text = current.text if isinstance(current, UserMessage) else str(current)
        current_images = current.images if isinstance(current, UserMessage) else None

        rebuilt = (
            "以下是我们之前的对话历史，请基于此上下文继续，"
            "不要重复或总结历史，直接回应用户的最新消息：\n\n"
            f"{history_text}\n\n"
            "---\n\n"
            f"用户最新消息：\n{current_text}"
        )
        return UserMessage(text=rebuilt, images=current_images)

    def _commit_message(self, record: AgentRecord, message: dict[str, Any]) -> int:
        record.messages.append(message)
        record.touch()
        save_agents(self.agents)
        return len(record.messages) - 1

    async def _emit_message_committed(
        self,
        emit: EventCallback,
        agent_id: str,
        record: AgentRecord,
        message: dict[str, Any],
        index: int,
    ) -> None:
        await emit(
            {
                "type": "message_committed",
                "agentId": agent_id,
                "message": message,
                "index": index,
                "messageCount": len(record.messages),
            }
        )

    async def send_message(
        self,
        agent_id: str,
        text: str,
        emit: EventCallback,
        attachments: list[dict[str, Any]] | None = None,
        display_content: str | None = None,
    ) -> None:
        record = self.agents.get(agent_id)
        if record is None:
            await emit({"type": "error", "message": "Agent 不存在"})
            return

        display_text = text.strip()
        if not display_text and not attachments:
            await emit({"type": "error", "agentId": agent_id, "message": "消息不能为空"})
            return

        async with self._lock_for(agent_id):
            self._cancel_requested[agent_id] = False
            self._pending_runs.add(agent_id)
            user_msg: dict[str, Any] = {
                "role": "user",
                "content": merge_user_display_content(display_content, display_text),
            }
            if attachments:
                user_msg["attachments"] = attachments
            index = self._commit_message(record, user_msg)
            await self._emit_message_committed(emit, agent_id, record, user_msg, index)
            await emit(
                {
                    "type": "run_started",
                    "agentId": agent_id,
                    "activity": "正在启动引擎…" if not self.is_started else "Agent 运行中…",
                }
            )

            user_message = self._build_user_message(
                record, display_text or "请查看引用的文件或目录。", attachments
            )

            try:
                await self._ensure_client()
                await self._execute_run_with_recovery(agent_id, record, user_message, emit)
            finally:
                self._pending_runs.discard(agent_id)
                self._cancel_requested.pop(agent_id, None)
                self._rebuilt_agents.discard(agent_id)

    async def _execute_run_with_recovery(
        self,
        agent_id: str,
        record: AgentRecord,
        user_message: UserMessage | str,
        emit: EventCallback,
    ) -> None:
        last_err: BaseException | None = None
        max_attempts = 3

        for attempt in range(max_attempts):
            if attempt == 1:
                self._reset_sdk_binding(record)
                self._mark_needs_history(record, reason="retry_after_failure")
                # 即使没有上文，也要新建会话；有上文则 _run_once 会注入历史
                self._rebuilt_agents.add(agent_id)
            elif attempt == 2:
                await self._restart_bridge()
                self._mark_needs_history(record, reason="retry_after_bridge_restart")
                self._rebuilt_agents.add(agent_id)

            try:
                await self._run_once(agent_id, record, user_message, emit)
                return
            except CursorAgentError as err:
                last_err = err
                if self._cancel_requested.get(agent_id):
                    return
                if attempt >= max_attempts - 1 or not self._is_recoverable_error(err):
                    break
                logger.warning(
                    "Agent %s 运行失败 (attempt %d/%d): %s",
                    agent_id,
                    attempt + 1,
                    max_attempts,
                    err,
                )
            except Exception as err:
                last_err = err
                if self._cancel_requested.get(agent_id):
                    return
                if attempt >= max_attempts - 1 or not self._is_bridge_error(err):
                    logger.exception("run failed")
                    break
                logger.warning(
                    "Agent %s bridge 异常 (attempt %d/%d): %s",
                    agent_id,
                    attempt + 1,
                    max_attempts,
                    err,
                )

        self._runs.pop(agent_id, None)
        self._rebuilt_agents.discard(agent_id)
        err_text = self._format_run_error(last_err)
        error_msg = {"role": "error", "content": err_text}
        index = self._commit_message(record, error_msg)
        await self._emit_message_committed(emit, agent_id, record, error_msg, index)
        await emit(
            {
                "type": "error",
                "agentId": agent_id,
                "message": err_text,
                "retryable": isinstance(last_err, CursorAgentError) and last_err.is_retryable,
            }
        )

    async def _conversation_fallback_text(self, run: Any) -> str:
        turns = await run.conversation()
        for turn in reversed(turns):
            if getattr(turn, "type", "") != "agentConversationTurn":
                continue
            agent_turn = getattr(turn, "turn", None)
            steps = getattr(agent_turn, "steps", ()) if agent_turn is not None else ()
            for step in reversed(steps):
                if getattr(step, "type", "") != "assistantMessage":
                    continue
                message = getattr(step, "message", None)
                text = getattr(message, "text", "") if message is not None else ""
                if str(text).strip():
                    return str(text).strip()
            break
        return ""

    def _assistant_message_from_segments(
        self, segments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        finalized = finalize_segments(segments)
        return {
            "role": "assistant",
            "content": segments_content(finalized),
            "segments": finalized,
            "blocks": segments_legacy_blocks(finalized),
        }

    async def _finalize_assistant_message(
        self,
        *,
        segments: list[dict[str, Any]],
        result: Any,
        run: Any,
        record: AgentRecord,
        agent_id: str,
    ) -> dict[str, Any]:
        assistant_msg = self._assistant_message_from_segments(segments)
        if assistant_msg["content"] or assistant_msg["blocks"]:
            return assistant_msg

        result_text = str(getattr(result, "result", "") or "").strip()
        if result_text:
            append_text_segment(segments, result_text)
            return self._assistant_message_from_segments(segments)

        try:
            fallback = await self._conversation_fallback_text(run)
        except Exception:
            logger.exception("读取 run conversation 失败: agent=%s", agent_id)
            fallback = ""
        if fallback:
            append_text_segment(segments, fallback)
            return self._assistant_message_from_segments(segments)

        logger.warning(
            "Agent %s 运行结束但无输出 (status=%s, run=%s)，视为会话失效并重试",
            agent_id,
            getattr(result, "status", ""),
            getattr(result, "id", ""),
        )
        raise _AgentSilentError()

    async def _run_once(
        self,
        agent_id: str,
        record: AgentRecord,
        user_message: UserMessage | str,
        emit: EventCallback,
    ) -> None:
        sdk_agent = await self._ensure_sdk_agent(record)

        if agent_id in self._rebuilt_agents:
            user_message = self._rebuild_message_with_history(record, user_message)

        run = await sdk_agent.send(user_message)
        self._runs[agent_id] = run
        await emit(
            {
                "type": "run_started",
                "agentId": agent_id,
                "runId": run.id,
                "sdkAgentId": sdk_agent.agent_id,
                "activity": "Agent 运行中…",
            }
        )

        if self._cancel_requested.get(agent_id):
            await self._abort_run(agent_id, run)
            return

        segments: list[dict[str, Any]] = []
        event_count = 0
        async for event in run.events():
            event_count += 1
            if event_count == 1:
                msg = getattr(event, "sdk_message", None)
                logger.info(
                    "Agent %s 首个流式事件 kind=%s type=%s",
                    agent_id,
                    getattr(event, "kind", ""),
                    getattr(msg, "type", "") if msg is not None else "",
                )
            if self._cancel_requested.get(agent_id):
                break
            for payload in serialize_run_event(event):
                apply_payload_to_segments(segments, payload)
                await emit(
                    {
                        "type": "stream",
                        "agentId": agent_id,
                        **payload,
                    }
                )

        if self._cancel_requested.get(agent_id):
            await self._abort_run(agent_id, run)
            return

        result = await run.wait()
        self._runs.pop(agent_id, None)
        logger.info(
            "Agent %s run 结束 status=%s events=%s run=%s",
            agent_id,
            getattr(result, "status", ""),
            event_count,
            getattr(result, "id", ""),
        )

        assistant_msg = await self._finalize_assistant_message(
            segments=segments,
            result=result,
            run=run,
            record=record,
            agent_id=agent_id,
        )

        if agent_id in self._rebuilt_agents:
            self._rebuilt_agents.discard(agent_id)

        index = self._commit_message(record, assistant_msg)
        await self._emit_message_committed(emit, agent_id, record, assistant_msg, index)

        await emit(
            {
                "type": "run_finished",
                "agentId": agent_id,
                "status": result.status,
                "runId": result.id,
                "messageCount": len(record.messages),
            }
        )

    async def _abort_run(self, agent_id: str, run: Any) -> None:
        self._runs.pop(agent_id, None)
        await _safe_cancel_run(run, agent_id=agent_id)

    async def cancel(self, agent_id: str, emit: EventCallback) -> None:
        self._cancel_requested[agent_id] = True
        run = self._runs.get(agent_id)
        if run is not None:
            await self._abort_run(agent_id, run)
        await emit({"type": "run_cancelled", "agentId": agent_id})
