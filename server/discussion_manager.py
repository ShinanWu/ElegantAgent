from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from cursor_sdk import AgentOptions, LocalAgentOptions, SendOptions, UserMessage
from cursor_sdk.types import SandboxOptions

from .agents import AgentRecord
from .bridge_env import workspace_path
from .discussions import Discussion, load_discussions, new_discussion, save_discussions
from .prompt_builder import build_discussion_prompt
from .stream_format import (
    apply_payload_to_segments,
    finalize_segments,
    segments_content,
    segments_legacy_blocks,
    serialize_run_event,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DiscussionManager:
    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._discussions = load_discussions()
        self._sdk_agents: dict[str, Any] = {}
        self._runs: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancel_requested: dict[str, bool] = {}

    def list_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        items = [
            d for d in self._discussions.values() if d.agent_id == agent_id
        ]
        items.sort(key=lambda d: d.created_at)
        return [d.to_dict() for d in items]

    def get(self, discussion_id: str) -> Discussion | None:
        return self._discussions.get(discussion_id)

    def create(self, agent_id: str, anchor: dict[str, Any]) -> Discussion:
        record = new_discussion(agent_id, anchor)
        self._discussions[record.id] = record
        save_discussions(self._discussions)
        return record

    def delete_for_agent(self, agent_id: str) -> None:
        to_remove = [did for did, d in self._discussions.items() if d.agent_id == agent_id]
        for did in to_remove:
            self.delete(did)

    def delete(self, discussion_id: str) -> Discussion | None:
        discussion = self._discussions.pop(discussion_id, None)
        if discussion is None:
            return None
        self._cancel_requested[discussion_id] = True
        run = self._runs.pop(discussion_id, None)
        if run is not None and run.supports("cancel"):
            try:
                asyncio.create_task(run.cancel())
            except Exception:
                pass
        self._cancel_requested.pop(discussion_id, None)
        sdk = self._sdk_agents.pop(discussion_id, None)
        if sdk is not None:
            asyncio.create_task(sdk.close())
        save_discussions(self._discussions)
        return discussion

    async def stop_all(self) -> None:
        for did in list(self._runs.keys()):
            self._cancel_requested[did] = True
            run = self._runs.pop(did, None)
            if run is not None and run.supports("cancel"):
                try:
                    await run.cancel()
                except Exception:
                    pass
        self._runs.clear()
        for agent in list(self._sdk_agents.values()):
            try:
                await agent.close()
            except Exception:
                pass
        self._sdk_agents.clear()

    def _lock_for(self, discussion_id: str) -> asyncio.Lock:
        if discussion_id not in self._locks:
            self._locks[discussion_id] = asyncio.Lock()
        return self._locks[discussion_id]

    async def _ensure_ephemeral_agent(self, record: AgentRecord, discussion_id: str) -> Any:
        if discussion_id in self._sdk_agents:
            return self._sdk_agents[discussion_id]

        client = await self._manager._ensure_client()
        options = AgentOptions(
            api_key=self._manager.api_key,
            model=record.model,
            mode="agent",
            local=LocalAgentOptions(
                cwd=workspace_path(record.cwd),
                sandbox_options=SandboxOptions(enabled=False),
            ),
        )
        agent = await client.agents.create(options)
        self._sdk_agents[discussion_id] = agent
        return agent

    async def send_message(
        self,
        discussion_id: str,
        text: str,
        emit: EventCallback,
    ) -> None:
        discussion = self._discussions.get(discussion_id)
        if discussion is None:
            await emit({"type": "error", "message": "讨论不存在"})
            return

        agent_record: AgentRecord | None = self._manager.agents.get(discussion.agent_id)
        if agent_record is None:
            await emit({"type": "error", "message": "Agent 不存在"})
            return

        async with self._lock_for(discussion_id):
            self._cancel_requested[discussion_id] = False
            user_msg = {"role": "user", "content": text.strip()}
            discussion.messages.append(user_msg)
            discussion.touch()
            save_discussions(self._discussions)
            await emit(
                {
                    "type": "discussion_user_message",
                    "discussionId": discussion_id,
                    "agentId": discussion.agent_id,
                    "message": user_msg,
                }
            )

            prompt = build_discussion_prompt(
                agent_record,
                discussion.anchor,
                text,
                discussion.messages[:-1],
            )

            try:
                sdk_agent = await self._ensure_ephemeral_agent(agent_record, discussion_id)
                run = await sdk_agent.send(
                    UserMessage(text=prompt),
                    SendOptions(mode="agent"),
                )
                self._runs[discussion_id] = run

                segments: list[dict[str, Any]] = []
                async for event in run.events():
                    if self._cancel_requested.get(discussion_id):
                        break
                    for payload in serialize_run_event(event):
                        apply_payload_to_segments(segments, payload)
                        await emit(
                            {
                                "type": "discussion_stream",
                                "discussionId": discussion_id,
                                "agentId": discussion.agent_id,
                                **payload,
                            }
                        )

                if self._cancel_requested.get(discussion_id):
                    run = self._runs.pop(discussion_id, None)
                    if run is not None and run.supports("cancel"):
                        try:
                            await run.cancel()
                        except Exception:
                            pass
                    await emit(
                        {
                            "type": "discussion_cancelled",
                            "discussionId": discussion_id,
                        }
                    )
                    return

                await run.wait()
                self._runs.pop(discussion_id, None)

                finalized = finalize_segments(segments)
                if segments_content(finalized) or segments_legacy_blocks(finalized):
                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": segments_content(finalized) or "（已完成，无文本输出）",
                        "segments": finalized,
                        "blocks": segments_legacy_blocks(finalized),
                    }
                    discussion.messages.append(assistant_msg)
                    discussion.touch()
                    save_discussions(self._discussions)
                    await emit(
                        {
                            "type": "discussion_finished",
                            "discussionId": discussion_id,
                            "agentId": discussion.agent_id,
                            "message": assistant_msg,
                        }
                    )
                else:
                    await emit(
                        {
                            "type": "discussion_finished",
                            "discussionId": discussion_id,
                            "agentId": discussion.agent_id,
                        }
                    )
            except Exception as err:
                self._runs.pop(discussion_id, None)
                logger.exception("discussion send failed")
                await emit(
                    {
                        "type": "error",
                        "discussionId": discussion_id,
                        "message": str(err),
                    }
                )
            finally:
                self._cancel_requested.pop(discussion_id, None)
