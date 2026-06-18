from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


def looks_like_json_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        json.loads(stripped)
        return True
    except json.JSONDecodeError:
        return stripped.count('"') >= 4 and ":" in stripped


def tool_label(name: str, args: Any) -> str:
    args_map = args if isinstance(args, dict) else {}
    path = (
        args_map.get("path")
        or args_map.get("target_file")
        or args_map.get("file_path")
        or args_map.get("relative_workspace_path")
        or args_map.get("file")
    )
    command = args_map.get("command") or args_map.get("cmd")
    query = args_map.get("query") or args_map.get("pattern")

    labels = {
        "read": "读取文件",
        "read_file": "读取文件",
        "write": "写入文件",
        "write_file": "写入文件",
        "edit": "编辑文件",
        "edit_file": "编辑文件",
        "search_replace": "编辑文件",
        "delete_file": "删除文件",
        "list_dir": "列出目录",
        "glob_file_search": "搜索文件",
        "grep": "搜索代码",
        "codebase_search": "语义搜索",
        "run_terminal_cmd": "运行命令",
        "shell": "运行命令",
        "bash": "运行命令",
        "web_search": "网页搜索",
        "mcp": "调用工具",
    }
    base = labels.get(name, name.replace("_", " "))

    if path:
        return f"{base} · {_short_path(str(path))}"
    if command:
        return f"{base} · {_short_text(str(command), 80)}"
    if query:
        return f"{base} · {_short_text(str(query), 60)}"
    return base


def _short_path(path: str) -> str:
    path = path.replace("\\", "/")
    if len(path) <= 56:
        return path
    parts = path.split("/")
    if len(parts) >= 2:
        return f"…/{'/'.join(parts[-2:])}"
    return f"…{path[-48:]}"


def _short_text(text: str, limit: int) -> str:
    one_line = re.sub(r"\s+", " ", text).strip()
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


_TERMINAL_ACTIVITY = frozenset(
    {
        "finished",
        "completed",
        "done",
        "cancelled",
        "canceled",
        "error",
        "failed",
        "success",
        "idle",
    }
)


def _is_terminal_activity(text: str) -> bool:
    return text.strip().lower() in _TERMINAL_ACTIVITY


def stream_payload_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    single = payload.get("block")
    if isinstance(single, dict):
        blocks.append(single)
    for item in payload.get("blocks") or []:
        if isinstance(item, dict):
            blocks.append(item)
    return blocks


def finalize_assistant_blocks(
    thinking_blocks: list[dict[str, Any]],
    tool_blocks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = list(thinking_blocks)
    for block in tool_blocks.values():
        copy = dict(block)
        if copy.get("status") == "running":
            copy["status"] = "completed"
        finalized.append(copy)
    return finalized


def apply_payload_to_segments(
    segments: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    """按流式事件到达顺序累积 text / thinking / tool 片段。"""
    if payload.get("text"):
        chunk = str(payload["text"])
        if segments and segments[-1].get("type") == "text":
            segments[-1]["text"] += chunk
        else:
            segments.append({"type": "text", "text": chunk})
    for block in stream_payload_blocks(payload):
        if block.get("type") == "thinking":
            text = str(block.get("text") or "")
            if segments and segments[-1].get("type") == "thinking":
                segments[-1]["text"] += text
            else:
                segments.append({"type": "thinking", "text": text})
        elif block.get("type") == "tool":
            key = str(block.get("id") or block.get("name") or "tool")
            for seg in reversed(segments):
                if seg.get("type") != "tool":
                    continue
                seg_key = str(seg.get("id") or seg.get("name") or "tool")
                if seg_key == key:
                    seg.update({**block, "type": "tool"})
                    break
            else:
                segments.append({**block, "type": "tool"})


def finalize_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for seg in segments:
        copy = dict(seg)
        if copy.get("type") == "tool" and copy.get("status") == "running":
            copy["status"] = "completed"
        finalized.append(copy)
    return finalized


def segments_content(segments: list[dict[str, Any]]) -> str:
    return "".join(str(seg.get("text") or "") for seg in segments if seg.get("type") == "text")


def segments_legacy_blocks(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for seg in segments:
        if seg.get("type") == "thinking":
            blocks.append({"type": "thinking", "text": seg.get("text", "")})
        elif seg.get("type") == "tool":
            blocks.append(dict(seg))
    return blocks


def append_text_segment(segments: list[dict[str, Any]], text: str) -> None:
    chunk = str(text or "").strip()
    if not chunk:
        return
    if segments and segments[-1].get("type") == "text":
        segments[-1]["text"] += chunk
    else:
        segments.append({"type": "text", "text": chunk})


def _tool_block_from_wire(call_id: str, tool_call: Any, status: str) -> dict[str, Any]:
    mapping = tool_call if isinstance(tool_call, Mapping) else {}
    name = str(mapping.get("name") or mapping.get("tool") or "tool")
    args = mapping.get("args") or mapping.get("input")
    return {
        "type": "tool",
        "id": call_id or name,
        "name": name,
        "label": tool_label(name, args),
        "status": status,
    }


def serialize_interaction_update(update: Any) -> list[dict[str, Any]]:
    update_type = getattr(update, "type", None)
    if update_type is None and isinstance(update, Mapping):
        update_type = update.get("type")
    if not update_type:
        return []

    if update_type == "text-delta":
        text = getattr(update, "text", None)
        if text is None and isinstance(update, Mapping):
            text = update.get("text")
        if text:
            return [{"text": str(text), "messageType": "interaction"}]
    if update_type == "thinking-delta":
        text = getattr(update, "text", None)
        if text is None and isinstance(update, Mapping):
            text = update.get("text")
        if text:
            return [{"block": {"type": "thinking", "text": str(text)}}]
    if update_type == "tool-call-started":
        call_id = getattr(update, "call_id", "") or (
            update.get("callId") if isinstance(update, Mapping) else ""
        )
        tool_call = getattr(update, "tool_call", None) or (
            update.get("toolCall") if isinstance(update, Mapping) else None
        )
        return [{"block": _tool_block_from_wire(str(call_id or ""), tool_call, "running")}]
    if update_type == "tool-call-completed":
        call_id = getattr(update, "call_id", "") or (
            update.get("callId") if isinstance(update, Mapping) else ""
        )
        tool_call = getattr(update, "tool_call", None) or (
            update.get("toolCall") if isinstance(update, Mapping) else None
        )
        return [{"block": _tool_block_from_wire(str(call_id or ""), tool_call, "completed")}]
    return []


def serialize_conversation_step(step: Any) -> dict[str, Any] | None:
    step_type = getattr(step, "type", None)
    if step_type is None and isinstance(step, Mapping):
        step_type = step.get("type")

    if step_type == "assistantMessage":
        message = getattr(step, "message", None)
        text = getattr(message, "text", "") if message is not None else ""
        if not text and isinstance(step, Mapping):
            inner = step.get("message")
            if isinstance(inner, Mapping):
                text = inner.get("text", "")
        if text:
            return {"text": str(text), "messageType": "step"}
    if step_type == "thinkingMessage":
        message = getattr(step, "message", None)
        text = getattr(message, "text", "") if message is not None else ""
        if not text and isinstance(step, Mapping):
            inner = step.get("message")
            if isinstance(inner, Mapping):
                text = inner.get("text", "")
        if text:
            return {"block": {"type": "thinking", "text": str(text)}}
    if step_type == "toolCall":
        message = getattr(step, "message", None)
        if message is None and isinstance(step, Mapping):
            message = step.get("message")
        if isinstance(message, Mapping):
            name = str(message.get("name") or message.get("tool") or "tool")
            args = message.get("args") or message.get("input")
            call_id = str(message.get("callId") or message.get("call_id") or name)
            return {"block": _tool_block_from_wire(call_id, message, "completed")}
    return None


def serialize_run_event(event: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    sdk_message = getattr(event, "sdk_message", None)
    if sdk_message is not None:
        payload = serialize_stream_message(sdk_message)
        if payload:
            payloads.append(payload)
    interaction_update = getattr(event, "interaction_update", None)
    if interaction_update is not None:
        payloads.extend(serialize_interaction_update(interaction_update))
    step = getattr(event, "step", None)
    if step is not None:
        payload = serialize_conversation_step(step)
        if payload:
            payloads.append(payload)
    return payloads


def serialize_stream_message(message: Any) -> dict[str, Any] | None:
    msg_type = getattr(message, "type", None)

    if msg_type in ("system", "user", "request"):
        return None

    if msg_type == "thinking":
        text = getattr(message, "text", "") or ""
        if not text.strip():
            return None
        return {"block": {"type": "thinking", "text": text}}

    if msg_type == "status":
        text = getattr(message, "message", "") or getattr(message, "status", "")
        if not text:
            return None
        if _is_terminal_activity(str(text)):
            return None
        return {"activity": str(text), "activityRunning": True}

    if msg_type == "task":
        text = getattr(message, "text", "") or getattr(message, "status", "")
        if not text:
            return None
        if _is_terminal_activity(str(text)):
            return None
        return {"activity": str(text), "activityRunning": True}

    if msg_type == "tool_call":
        name = getattr(message, "name", "tool") or "tool"
        status = getattr(message, "status", "running") or "running"
        call_id = getattr(message, "call_id", "") or ""
        return {
            "block": {
                "type": "tool",
                "id": call_id or name,
                "name": name,
                "label": tool_label(name, getattr(message, "args", None)),
                "status": status,
            }
        }

    if msg_type == "tool_result":
        return None

    if msg_type == "assistant":
        inner = getattr(message, "message", message)
        content = getattr(inner, "content", [])
        text_parts: list[str] = []
        blocks: list[dict[str, Any]] = []

        for block in content:
            block_type = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if block_type == "text":
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text", "")
                text = str(text or "")
                if looks_like_json_noise(text):
                    continue
                if text:
                    text_parts.append(text)
            elif block_type == "thinking":
                text = getattr(block, "text", None)
                if text is None and isinstance(block, dict):
                    text = block.get("text", "")
                if text:
                    blocks.append({"type": "thinking", "text": str(text)})
            elif block_type in ("tool_use", "tool_call"):
                name = getattr(block, "name", None) or (
                    block.get("name") if isinstance(block, dict) else "tool"
                )
                args = getattr(block, "input", None)
                if args is None and isinstance(block, dict):
                    args = block.get("input") or block.get("args")
                blocks.append(
                    {
                        "type": "tool",
                        "name": str(name or "tool"),
                        "label": tool_label(str(name or "tool"), args),
                        "status": "completed",
                    }
                )

        payload: dict[str, Any] = {"messageType": msg_type}
        if text_parts:
            payload["text"] = "".join(text_parts)
        if blocks:
            payload["blocks"] = blocks
        if not payload.get("text") and not payload.get("blocks"):
            return None
        return payload

    return None
