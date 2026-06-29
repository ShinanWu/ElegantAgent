from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .agent_workspace import collect_memory, collect_rules, collect_skills, read_soul, resolve_attachment_path
from .agents import AgentRecord

MAX_SECTION_CHARS = 6000
MAX_ANCHOR_FILE_BYTES = 512_000
MAX_ANCHOR_FILE_CHARS = 8000

ATTACH_MARKER_RE = re.compile(r"\[\[πattach:(.*?)\]\]")
PLACEHOLDER_QUOTES = frozenset({"", "(引用路径)"})


def rebuild_marked_content(serialized: str, plain: str) -> str:
    """把 plain 全文与 serialized 中的附件标记按顺序合并。"""
    markers = list(ATTACH_MARKER_RE.finditer(serialized))
    if not markers:
        return plain or serialized

    result: list[str] = []
    plain_offset = 0
    last_serialized = 0

    for marker in markers:
        before_slice = serialized[last_serialized : marker.start()]
        before_inline = ATTACH_MARKER_RE.sub("", before_slice)
        if before_inline:
            take = len(before_inline)
            if plain_offset < len(plain):
                result.append(plain[plain_offset : plain_offset + take])
            else:
                result.append(before_inline)
            plain_offset += take
        result.append(marker.group(0))
        last_serialized = marker.end()

    after_inline = ATTACH_MARKER_RE.sub("", serialized[last_serialized:])
    if after_inline:
        take = len(after_inline)
        if plain_offset < len(plain):
            chunk = plain[plain_offset : plain_offset + take]
            result.append(chunk)
            plain_offset += take
            if len(chunk) < take:
                result.append(after_inline[len(chunk) :])
        else:
            result.append(after_inline)
    if plain_offset < len(plain):
        result.append(plain[plain_offset:])
    return "".join(result).strip()


def merge_user_display_content(display_content: str | None, plain: str) -> str:
    """合并展示用 content：含附件标记时以客户端 serialized 排版为准。"""
    dc = str(display_content or "").strip()
    pt = str(plain or "").strip()
    if not dc:
        return pt or "(引用路径)"
    if ATTACH_MARKER_RE.search(dc):
        return dc
    if not pt:
        return dc
    return dc if dc else pt

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic", ".heif"}
)


def _clip(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_files_section(title: str, files: list[dict[str, str]]) -> str:
    if not files:
        return ""
    parts = [f"[{title}]"]
    for item in files:
        parts.append(f"--- {item['path']} ---")
        parts.append(_clip(item.get("content", ""), 2000))
    return "\n".join(parts)


def build_prompt_text(record: AgentRecord, user_text: str) -> str:
    sections: list[str] = []

    soul = _clip(read_soul(record.cwd))
    if record.enable_soul and soul:
        sections.append("[Agent Soul — 永久设定，每次对话必须遵守]")
        sections.append(soul)

    if record.enable_rules:
        block = _format_files_section("Rules", collect_rules(record))
        if block:
            sections.append(block)

    if record.enable_skills:
        block = _format_files_section("Skills", collect_skills(record))
        if block:
            sections.append(block)

    if record.enable_memory:
        block = _format_files_section("Memory", collect_memory(record))
        if block:
            sections.append(block)

    sections.append("---")
    sections.append(
        "产物约定: 生成的图片、导出文件等请保存在当前工作目录内，"
        "优先使用 `.agent/outputs/`；用户上传在 `.agent/uploads/`。"
    )
    sections.append(f"用户消息:\n{user_text.strip()}")
    return "\n\n".join(sections)


def _attachment_ref_line(path: Path) -> str:
    if path.is_dir():
        return f"[引用目录: {path}]"
    return f"[引用文件: {path}]"


def _expand_attachment_markers(text: str, record: AgentRecord | None = None) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if not raw:
            return ""
        if record is not None:
            resolved = resolve_attachment_path(record, {"path": raw})
            if resolved is not None:
                return _attachment_ref_line(resolved)
        if raw.endswith("/"):
            return f"[引用目录: {raw.rstrip('/')}]"
        return f"[引用文件: {raw}]"

    return ATTACH_MARKER_RE.sub(repl, text).strip()


def _read_anchor_file_snippet(path: Path) -> str:
    if not path.is_file():
        return ""
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return f"(图片文件，路径: {path})"
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > MAX_ANCHOR_FILE_BYTES:
        return f"(文件过大，仅提供路径: {path}，{size} 字节)"
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"(无法读取文件: {path})"
    return _clip(data, MAX_ANCHOR_FILE_CHARS)


def _paths_from_anchor_text(text: str) -> list[str]:
    paths: list[str] = []
    for match in ATTACH_MARKER_RE.finditer(text):
        raw = match.group(1).strip()
        if raw:
            paths.append(raw.rstrip("/"))
    return paths


def _enrich_anchor_from_message(record: AgentRecord, anchor: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(anchor)
    quote = str(enriched.get("quote") or "").strip()
    if quote not in PLACEHOLDER_QUOTES:
        return enriched

    idx = enriched.get("messageIndex")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return enriched

    messages = record.messages
    if not (0 <= idx < len(messages)):
        return enriched

    msg = messages[idx]
    content = str(msg.get("content") or "").strip()
    if content and content not in PLACEHOLDER_QUOTES:
        if not enriched.get("quoteContent"):
            enriched["quoteContent"] = content
        enriched["quote"] = _expand_attachment_markers(content, record) or content

    if not enriched.get("attachments") and msg.get("attachments"):
        enriched["attachments"] = list(msg.get("attachments") or [])

    return enriched


def format_discussion_anchor(record: AgentRecord, anchor: dict[str, Any]) -> str:
    enriched = _enrich_anchor_from_message(record, anchor)
    quote_content = str(enriched.get("quoteContent") or "").strip()
    quote = str(enriched.get("quote") or "").strip()
    attachments = list(enriched.get("attachments") or [])

    if quote_content:
        body = _expand_attachment_markers(quote_content, record)
    elif quote not in PLACEHOLDER_QUOTES:
        body = (
            _expand_attachment_markers(quote, record)
            if ATTACH_MARKER_RE.search(quote)
            else quote
        )
    else:
        body = ""

    if body in PLACEHOLDER_QUOTES:
        body = ""

    resolved_paths: list[Path] = []
    seen_paths: set[str] = set()

    def add_path(path: Path | None) -> None:
        if path is None:
            return
        key = str(path)
        if key in seen_paths:
            return
        seen_paths.add(key)
        resolved_paths.append(path)

    for item in attachments:
        add_path(resolve_attachment_path(record, item))

    for raw in _paths_from_anchor_text(quote_content or quote):
        add_path(resolve_attachment_path(record, {"path": raw}))

    ref_lines: list[str] = []
    embed_blocks: list[str] = []
    for path in resolved_paths:
        ref_line = _attachment_ref_line(path)
        if ref_line not in ref_lines:
            ref_lines.append(ref_line)
        if path.is_file():
            snippet = _read_anchor_file_snippet(path)
            if snippet:
                embed_blocks.append(f"--- 文件内容: {path} ---\n{snippet}")

    if not body and ref_lines:
        body = "\n".join(ref_lines)
    elif body and ref_lines:
        missing = [line for line in ref_lines if line not in body]
        if missing:
            body = body + "\n" + "\n".join(missing)

    if embed_blocks:
        body = (body + "\n\n" if body else "") + "\n\n".join(embed_blocks)

    return body.strip() or "(未能解析引用内容，请用户提供更多上下文)"


def build_discussion_prompt(
    record: AgentRecord,
    anchor: dict[str, Any],
    question: str,
    thread_messages: list[dict[str, Any]] | None = None,
) -> str:
    anchor_text = format_discussion_anchor(record, anchor)
    parts = [
        "[讨论模式 — 只读问答]",
        "你只能解释、分析、回答用户问题，不会修改主会话。",
        "允许使用只读工具（如 read_file、grep、codebase_search、list_dir、glob 等）读取代码和文件。",
        "禁止修改/写入/删除文件，禁止运行 shell 或终端命令，禁止任何会改变工作区的操作。",
        "引用原文是讨论锚点；若需要更多上下文，请主动读取相关路径。",
        "",
        f"Agent 名称: {record.name}",
        f"工作目录: {record.cwd}",
        "",
        "引用原文:",
        anchor_text,
        "",
    ]
    soul = _clip(read_soul(record.cwd), 3000)
    if record.enable_soul and soul:
        parts.extend(["Agent Soul:", soul, ""])

    if thread_messages:
        parts.append("讨论历史:")
        for msg in thread_messages[-6:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"{role}: {content}")

    parts.extend(["", f"用户问题:\n{question.strip()}"])
    return "\n\n".join(parts)


def _discussion_title(anchor: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> str:
    quote = str(anchor.get("quoteContent") or anchor.get("quote") or "")
    quote = ATTACH_MARKER_RE.sub(
        lambda m: (m.group(1).rstrip("/").split("/")[-1] or m.group(1)),
        quote,
    )
    quote = re.sub(r"\s+", " ", quote).strip()
    if quote and quote not in PLACEHOLDER_QUOTES:
        return quote[:72] + ("…" if len(quote) > 72 else "")
    attachments = anchor.get("attachments") or []
    if attachments:
        names = [str(a.get("name") or a.get("path") or "附件") for a in attachments]
        return " · ".join(names)[:72]
    for msg in messages or []:
        if msg.get("role") == "user" and msg.get("content"):
            content = str(msg["content"]).strip()
            return content[:72] + ("…" if len(content) > 72 else "")
    return "讨论"


NO_INSTRUCTION_TEXT = "（未识别到意图）"
NO_INSTRUCTION_LEGACY = frozenset(
    {
        "（无指令意图）",
        "无指令意图",
        "（无意图）",
        "（未识别到意图）",
    }
)


def is_no_instruction_summary(text: str | None) -> bool:
    t = str(text or "").strip()
    return t == NO_INSTRUCTION_TEXT or t in NO_INSTRUCTION_LEGACY


def normalize_instruction_summary(text: str | None) -> str:
    raw = str(text or "").strip()
    if not raw or is_no_instruction_summary(raw):
        return NO_INSTRUCTION_TEXT
    return raw


def build_discussion_summary_prompt(record: AgentRecord, discussion: Any) -> str:
    anchor_text = format_discussion_anchor(record, discussion.anchor)
    parts = [
        "[讨论意图提炼 — 只读]",
        "你的任务：判断下方讨论是否包含用户要对主 Agent 表达的明确下一步意图；若有，写一段可直接粘贴进主对话框的用户话术。",
        "",
        "输出要求：",
        "- 仅当讨论中出现明确的待办、决策、执行请求或用户已表达要继续做的动作时，才输出意图话术",
        "- 以用户第一人称、祈使口吻书写（例如「请…」「帮我…」「继续…」）",
        "- 不要对讨论做客观复述，不要写「本讨论…」「双方认为…」",
        "- 不要前言、后缀或 markdown 标题",
        "- 禁止在无明确意图时编造「请继续…」「请记住…」等兜底内容",
        f"- 系统未识别到明确意图时，只输出 exactly：{NO_INSTRUCTION_TEXT}（括号表示系统提示，非用户意图）",
        "",
        f"讨论主题: {_discussion_title(discussion.anchor, discussion.messages)}",
        "",
        "引用原文:",
        anchor_text,
        "",
    ]
    if discussion.messages:
        parts.append("讨论内容:")
        for msg in discussion.messages:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content") or "").strip()
            if content:
                parts.append(f"{role}: {content}")
        parts.append("")
    else:
        parts.append(f"（尚无追问；若引用原文本身也不含明确执行意图，输出：{NO_INSTRUCTION_TEXT}）")
        parts.append("")
    return "\n".join(parts)


def build_combined_summary_prompt(
    record: AgentRecord,
    items: list[dict[str, str]],
) -> str:
    parts = [
        "[合并意图提炼 — 只读]",
        "以下是多条讨论各自提炼出的意图草稿。请合并为一段用户要对主 Agent 说的话。",
        "",
        "输出要求：",
        f"- 仅合并含明确行动意图的草稿；草稿为 {NO_INSTRUCTION_TEXT} 或旧版系统标记的条目直接跳过",
        f"- 若全部草稿均为系统未识别标记，只输出：{NO_INSTRUCTION_TEXT}",
        "- 有可合并的意图时：第一人称、祈使口吻，去重合并，可直接发送",
        "- 不要前言、后缀，不要编造草稿中未出现的行动",
        "",
    ]
    for idx, item in enumerate(items, start=1):
        parts.append(f"--- 讨论{idx}（{item.get('title', '讨论')}）的意图草稿 ---")
        parts.append(item.get("summary", "").strip() or NO_INSTRUCTION_TEXT)
        parts.append("")
    return "\n".join(parts).strip()
