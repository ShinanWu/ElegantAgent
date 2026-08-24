from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from cursor_sdk import AgentOptions, LocalAgentOptions, SendOptions, UserMessage
from cursor_sdk.types import SandboxOptions

from .agents import AgentRecord
from .bridge_env import workspace_path
from .combined_summaries import (
    CombinedSummary,
    load_combined_summaries,
    new_combined_summary,
    save_combined_summaries,
)
from .discussions import Discussion, load_discussions, new_discussion, save_discussions
from .prompt_builder import (
    NO_INSTRUCTION_TEXT,
    _discussion_title,
    build_combined_summary_prompt,
    build_discussion_prompt,
    build_discussion_summary_prompt,
    is_no_instruction_summary,
    normalize_instruction_summary,
)
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
        self._combined = load_combined_summaries()
        self._sdk_agents: dict[str, Any] = {}
        self._runs: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._summary_locks: dict[str, asyncio.Lock] = {}
        self._combined_locks: dict[str, asyncio.Lock] = {}
        self._cancel_requested: dict[str, bool] = {}
        self._summarizing: set[str] = set()
        self._combined_summarizing: set[str] = set()

    def list_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        items = [
            d for d in self._discussions.values() if d.agent_id == agent_id
        ]
        items.sort(key=lambda d: d.created_at)
        return [d.to_dict() for d in items]

    def get(self, discussion_id: str) -> Discussion | None:
        return self._discussions.get(discussion_id)

    def set_collapsed(self, discussion_id: str, collapsed: bool) -> Discussion | None:
        discussion = self._discussions.get(discussion_id)
        if discussion is None:
            return None
        if not collapsed:
            for other in self._discussions.values():
                if other.agent_id == discussion.agent_id and other.id != discussion_id:
                    other.collapsed = True
        discussion.collapsed = collapsed
        save_discussions(self._discussions)
        return discussion

    def create(self, agent_id: str, anchor: dict[str, Any]) -> Discussion:
        for existing in self._discussions.values():
            if existing.agent_id == agent_id:
                existing.collapsed = True
        record = new_discussion(agent_id, anchor)
        record.collapsed = False
        self._discussions[record.id] = record
        save_discussions(self._discussions)
        return record

    def delete_for_agent(self, agent_id: str) -> None:
        to_remove = [did for did, d in self._discussions.items() if d.agent_id == agent_id]
        for did in to_remove:
            self.delete(did, save=False)
        self._delete_combined_for_agent(agent_id)
        save_discussions(self._discussions)

    def _delete_combined_for_agent(self, agent_id: str) -> None:
        to_remove = [cid for cid, cs in self._combined.items() if cs.agent_id == agent_id]
        for cid in to_remove:
            self._combined.pop(cid, None)
            self._combined_locks.pop(cid, None)
            sdk_key = f"combined:{cid}"
            sdk = self._sdk_agents.pop(sdk_key, None)
            if sdk is not None:
                asyncio.create_task(sdk.close())
        if to_remove:
            save_combined_summaries(self._combined)

    def delete(self, discussion_id: str, *, save: bool = True) -> Discussion | None:
        discussion = self._discussions.pop(discussion_id, None)
        if discussion is None:
            return None
        self._cancel_requested[discussion_id] = True
        run = self._runs.get(discussion_id)
        if run is not None and run.supports("cancel"):
            try:
                asyncio.create_task(run.cancel())
            except Exception:
                logger.debug("cancel discussion run failed", exc_info=True)
        if discussion_id not in self._runs:
            self._cancel_requested.pop(discussion_id, None)
        sdk = self._sdk_agents.pop(discussion_id, None)
        if sdk is not None:
            asyncio.create_task(sdk.close())
        combined_remove = [
            cid
            for cid, cs in self._combined.items()
            if discussion_id in cs.discussion_ids
        ]
        for cid in combined_remove:
            self._combined.pop(cid, None)
            self._combined_locks.pop(cid, None)
            sdk_key = f"combined:{cid}"
            sdk = self._sdk_agents.pop(sdk_key, None)
            if sdk is not None:
                asyncio.create_task(sdk.close())
        if combined_remove:
            save_combined_summaries(self._combined)
        if save:
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
            tools=["read", "grep", "glob", "ls"],
            local=LocalAgentOptions(
                cwd=workspace_path(record.cwd),
                sandbox_options=SandboxOptions(enabled=True),
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
            await emit(
                {
                    "type": "error",
                    "discussionId": discussion_id,
                    "scope": "discussion",
                    "message": "讨论不存在",
                }
            )
            return

        agent_record: AgentRecord | None = self._manager.agents.get(discussion.agent_id)
        if agent_record is None:
            await emit(
                {
                    "type": "error",
                    "discussionId": discussion_id,
                    "scope": "discussion",
                    "message": "Agent 不存在",
                }
            )
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

                cancelled = bool(self._cancel_requested.get(discussion_id)) or (
                    discussion_id not in self._discussions
                )
                if cancelled:
                    run = self._runs.pop(discussion_id, None)
                    if run is not None and run.supports("cancel"):
                        try:
                            await run.cancel()
                        except Exception:
                            logger.debug("cancel discussion run failed", exc_info=True)
                    await emit(
                        {
                            "type": "discussion_cancelled",
                            "discussionId": discussion_id,
                            "agentId": discussion.agent_id,
                        }
                    )
                    return

                if discussion_id not in self._discussions:
                    self._runs.pop(discussion_id, None)
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
                        "scope": "discussion",
                        "message": str(err),
                    }
                )
            finally:
                self._cancel_requested.pop(discussion_id, None)

    def list_combined_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        items = [cs for cs in self._combined.values() if cs.agent_id == agent_id]
        items.sort(key=lambda cs: cs.created_at, reverse=True)
        return [cs.to_dict() for cs in items]

    def _summary_lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._summary_locks:
            self._summary_locks[key] = asyncio.Lock()
        return self._summary_locks[key]

    def _combined_lock_for(self, combined_id: str) -> asyncio.Lock:
        if combined_id not in self._combined_locks:
            self._combined_locks[combined_id] = asyncio.Lock()
        return self._combined_locks[combined_id]

    async def _stream_text(
        self,
        sdk_agent: Any,
        prompt: str,
        emit: EventCallback,
        stream_type: str,
        stream_extra: dict[str, Any],
    ) -> str:
        run = await sdk_agent.send(UserMessage(text=prompt), SendOptions(mode="agent"))
        segments: list[dict[str, Any]] = []
        async for event in run.events():
            for payload in serialize_run_event(event):
                apply_payload_to_segments(segments, payload)
                text_patch = payload.get("text")
                if text_patch:
                    await emit({"type": stream_type, **stream_extra, "text": text_patch})
        await run.wait()
        finalized = finalize_segments(segments)
        return (segments_content(finalized) or "").strip()

    async def summarize_discussion(
        self,
        discussion_id: str,
        emit: EventCallback,
        *,
        regenerate: bool = False,
    ) -> str | None:
        discussion = self._discussions.get(discussion_id)
        if discussion is None:
            await emit({"type": "error", "message": "讨论不存在"})
            return None

        agent_record = self._manager.agents.get(discussion.agent_id)
        if agent_record is None:
            await emit({"type": "error", "message": "Agent 不存在"})
            return None

        if discussion.summary and not regenerate:
            return discussion.summary

        if discussion_id in self._summarizing:
            await emit(
                {
                    "type": "error",
                    "discussionId": discussion_id,
                    "scope": "summary",
                    "message": "总结进行中",
                }
            )
            return None

        async with self._summary_lock_for(discussion_id):
            if discussion.summary and not regenerate:
                return discussion.summary

            self._summarizing.add(discussion_id)
            await emit({"type": "discussion_summary_started", "discussionId": discussion_id})

            try:
                prompt = build_discussion_summary_prompt(agent_record, discussion)
                sdk_key = f"summary:{discussion_id}"
                sdk_agent = await self._ensure_ephemeral_agent(agent_record, sdk_key)
                text = await self._stream_text(
                    sdk_agent,
                    prompt,
                    emit,
                    "discussion_summary_stream",
                    {"discussionId": discussion_id, "agentId": discussion.agent_id},
                )
                discussion.summary = normalize_instruction_summary(text)
                discussion.summary_updated_at = datetime.now(timezone.utc).isoformat()
                discussion.touch()
                save_discussions(self._discussions)
                await emit(
                    {
                        "type": "discussion_summary_updated",
                        "discussion": discussion.to_dict(),
                    }
                )
                return discussion.summary
            except Exception as err:
                logger.exception("discussion summarize failed")
                await emit(
                    {
                        "type": "error",
                        "discussionId": discussion_id,
                        "scope": "summary",
                        "message": str(err),
                    }
                )
                return None
            finally:
                self._summarizing.discard(discussion_id)

    async def _ensure_discussion_summary(
        self,
        discussion_id: str,
        emit: EventCallback,
        *,
        regenerate: bool = False,
    ) -> str | None:
        discussion = self._discussions.get(discussion_id)
        if discussion is None:
            return None
        if discussion.summary and not regenerate:
            return discussion.summary
        return await self.summarize_discussion(discussion_id, emit, regenerate=regenerate)

    def update_discussion_summary(self, discussion_id: str, text: str) -> Discussion | None:
        discussion = self._discussions.get(discussion_id)
        if discussion is None:
            return None
        discussion.summary = text.strip()
        discussion.summary_updated_at = datetime.now(timezone.utc).isoformat()
        discussion.touch()
        save_discussions(self._discussions)
        return discussion

    async def summarize_discussions(
        self,
        agent_id: str,
        discussion_ids: list[str],
        emit: EventCallback,
        *,
        regenerate: bool = False,
    ) -> CombinedSummary | None:
        agent_record = self._manager.agents.get(agent_id)
        if agent_record is None:
            await emit({"type": "error", "message": "Agent 不存在"})
            return None

        ids = [did for did in discussion_ids if did in self._discussions]
        if not ids:
            await emit({"type": "error", "message": "未选择有效讨论"})
            return None

        combined = new_combined_summary(agent_id, ids)
        self._combined[combined.id] = combined
        save_combined_summaries(self._combined)

        async with self._combined_lock_for(combined.id):
            if combined.id in self._combined_summarizing:
                await emit({"type": "error", "message": "合并总结进行中"})
                return None
            self._combined_summarizing.add(combined.id)

            try:
                await emit(
                    {
                        "type": "combined_summary_started",
                        "combinedSummary": combined.to_dict(),
                    }
                )
                total = len(ids)
                summaries: list[dict[str, str]] = []

                for idx, did in enumerate(ids, start=1):
                    discussion = self._discussions.get(did)
                    if discussion is None:
                        continue
                    await emit(
                        {
                            "type": "combined_summary_progress",
                            "combinedSummaryId": combined.id,
                            "phase": "individual",
                            "current": idx,
                            "total": total,
                            "discussionId": did,
                        }
                    )
                    summary_text = await self._ensure_discussion_summary(
                        did, emit, regenerate=regenerate
                    )
                    if summary_text is None:
                        summary_text = discussion.summary or NO_INSTRUCTION_TEXT
                    summaries.append(
                        {
                            "title": _discussion_title(discussion.anchor, discussion.messages),
                            "summary": summary_text,
                        }
                    )

                await emit(
                    {
                        "type": "combined_summary_progress",
                        "combinedSummaryId": combined.id,
                        "phase": "merge",
                        "current": total,
                        "total": total,
                    }
                )

                prompt = build_combined_summary_prompt(agent_record, summaries)
                sdk_key = f"combined:{combined.id}"
                sdk_agent = await self._ensure_ephemeral_agent(agent_record, sdk_key)
                text = await self._stream_text(
                    sdk_agent,
                    prompt,
                    emit,
                    "combined_summary_stream",
                    {"combinedSummaryId": combined.id, "agentId": agent_id},
                )
                combined.summary = normalize_instruction_summary(text)
                combined.touch()
                save_combined_summaries(self._combined)
                await emit(
                    {
                        "type": "combined_summary_updated",
                        "combinedSummary": combined.to_dict(),
                    }
                )
                return combined
            except Exception as err:
                logger.exception("combined summarize failed")
                await emit(
                    {
                        "type": "error",
                        "combinedSummaryId": combined.id,
                        "scope": "combined",
                        "message": str(err),
                    }
                )
                return None
            finally:
                self._combined_summarizing.discard(combined.id)

    def update_combined_summary(self, combined_id: str, text: str) -> CombinedSummary | None:
        combined = self._combined.get(combined_id)
        if combined is None:
            return None
        combined.summary = text.strip()
        combined.touch()
        save_combined_summaries(self._combined)
        return combined

    async def resummarize_combined(
        self,
        combined_id: str,
        emit: EventCallback,
        *,
        regenerate_individuals: bool = False,
    ) -> CombinedSummary | None:
        combined = self._combined.get(combined_id)
        if combined is None:
            await emit({"type": "error", "message": "合并总结不存在"})
            return None

        agent_record = self._manager.agents.get(combined.agent_id)
        if agent_record is None:
            await emit({"type": "error", "message": "Agent 不存在"})
            return None

        if combined_id in self._combined_summarizing:
            await emit({"type": "error", "message": "合并总结进行中"})
            return None

        async with self._combined_lock_for(combined_id):
            self._combined_summarizing.add(combined_id)
            try:
                ids = [did for did in combined.discussion_ids if did in self._discussions]
                if not ids:
                    await emit({"type": "error", "message": "关联讨论已不存在"})
                    return None

                summaries: list[dict[str, str]] = []
                total = len(ids)

                if regenerate_individuals:
                    for idx, did in enumerate(ids, start=1):
                        discussion = self._discussions.get(did)
                        if discussion is None:
                            continue
                        await emit(
                            {
                                "type": "combined_summary_progress",
                                "combinedSummaryId": combined_id,
                                "phase": "individual",
                                "current": idx,
                                "total": total,
                                "discussionId": did,
                            }
                        )
                        summary_text = await self.summarize_discussion(
                            did, emit, regenerate=True
                        )
                        if summary_text is None:
                            summary_text = discussion.summary or NO_INSTRUCTION_TEXT
                        summaries.append(
                            {
                                "title": _discussion_title(discussion.anchor, discussion.messages),
                                "summary": summary_text,
                            }
                        )
                else:
                    for did in ids:
                        discussion = self._discussions.get(did)
                        if discussion is None:
                            continue
                        summary_text = discussion.summary
                        if not summary_text:
                            summary_text = await self._ensure_discussion_summary(did, emit)
                        summaries.append(
                            {
                                "title": _discussion_title(discussion.anchor, discussion.messages),
                                "summary": summary_text or NO_INSTRUCTION_TEXT,
                            }
                        )

                combined.summary = ""
                combined.touch()
                save_combined_summaries(self._combined)

                await emit(
                    {
                        "type": "combined_summary_progress",
                        "combinedSummaryId": combined_id,
                        "phase": "merge",
                        "current": total,
                        "total": total,
                    }
                )

                prompt = build_combined_summary_prompt(agent_record, summaries)
                sdk_key = f"combined:{combined_id}"
                sdk_agent = await self._ensure_ephemeral_agent(agent_record, sdk_key)
                text = await self._stream_text(
                    sdk_agent,
                    prompt,
                    emit,
                    "combined_summary_stream",
                    {"combinedSummaryId": combined_id, "agentId": combined.agent_id},
                )
                combined.summary = normalize_instruction_summary(text)
                combined.touch()
                save_combined_summaries(self._combined)
                await emit(
                    {
                        "type": "combined_summary_updated",
                        "combinedSummary": combined.to_dict(),
                    }
                )
                return combined
            except Exception as err:
                logger.exception("resummarize combined failed")
                await emit(
                    {
                        "type": "error",
                        "combinedSummaryId": combined_id,
                        "scope": "combined",
                        "message": str(err),
                    }
                )
                return None
            finally:
                self._combined_summarizing.discard(combined_id)

    def delete_combined_summary(self, combined_id: str) -> CombinedSummary | None:
        combined = self._combined.pop(combined_id, None)
        if combined is None:
            return None
        self._combined_locks.pop(combined_id, None)
        sdk_key = f"combined:{combined_id}"
        sdk = self._sdk_agents.pop(sdk_key, None)
        if sdk is not None:
            asyncio.create_task(sdk.close())
        save_combined_summaries(self._combined)
        return combined
