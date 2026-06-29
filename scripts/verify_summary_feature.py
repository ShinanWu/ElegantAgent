#!/usr/bin/env python3
"""验证讨论总结 / 合并总结功能的数据层与静态集成（无需 Cursor API）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    line = f"  {mark} {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok:
        FAILURES.append(f"{name}: {detail}" if detail else name)


FAILURES: list[str] = []


def verify_models() -> None:
    print("==> 1. 数据模型持久化")
    from server.discussions import Discussion, save_discussions, load_discussions
    from server.combined_summaries import (
        CombinedSummary,
        save_combined_summaries,
        load_combined_summaries,
        new_combined_summary,
    )

    d = Discussion(
        id="d1",
        agent_id="a1",
        anchor={"quote": "测试引用"},
        summary="单条总结",
        summary_updated_at="2026-01-01T00:00:00+00:00",
    )
    payload = d.to_dict()
    check("Discussion.to_dict 含 summary", payload.get("summary") == "单条总结")
    check("Discussion.to_dict camelCase", payload.get("summaryUpdatedAt") is not None)

    cs = new_combined_summary("a1", ["d1", "d2"])
    cs.summary = "合并内容"
    check("CombinedSummary discussionIds", cs.to_dict()["discussionIds"] == ["d1", "d2"])

    with tempfile.TemporaryDirectory() as tmp:
        disc_path = Path(tmp) / "discussions.json"
        comb_path = Path(tmp) / "combined_summaries.json"
        with patch("server.discussions.discussions_file", return_value=disc_path):
            save_discussions({"d1": d})
            loaded = load_discussions()["d1"]
            check("Discussion 读写 summary", loaded.summary == "单条总结")
        with patch("server.combined_summaries.combined_summaries_file", return_value=comb_path):
            save_combined_summaries({cs.id: cs})
            loaded_cs = load_combined_summaries()[cs.id]
            check("CombinedSummary 读写", loaded_cs.summary == "合并内容")


def verify_prompts() -> None:
    print("==> 2. Prompt 构建")
    from server.agents import AgentRecord
    from server.discussions import Discussion
    from server.prompt_builder import (
        NO_INSTRUCTION_TEXT,
        build_discussion_summary_prompt,
        build_combined_summary_prompt,
    )

    record = AgentRecord(id="a1", name="Test", cwd="/tmp", model="composer-2.5")
    discussion = Discussion(
        id="d1",
        agent_id="a1",
        anchor={"quote": "hello world"},
        messages=[{"role": "user", "content": "这是什么？"}],
    )
    p1 = build_discussion_summary_prompt(record, discussion)
    check("单条 prompt 无意图时输出固定文案", NO_INSTRUCTION_TEXT in p1)
    check("单条 prompt 禁止兜底编造", "禁止" in p1 and "兜底" in p1)
    check("单条 prompt 含引用", "引用原文" in p1 and "hello world" in p1)
    check("单条 prompt 含讨论", "这是什么？" in p1)

    p2 = build_combined_summary_prompt(
        record,
        [
            {"title": "讨论A", "summary": "请帮我继续实现 A"},
            {"title": "讨论B", "summary": "请帮我修复 B"},
        ],
    )
    check("合并 prompt 用意图草稿", "请帮我继续实现 A" in p2 and "请帮我修复 B" in p2)
    check("合并 prompt 不含用户问题原文", "这是什么？" not in p2)
    check("合并 prompt 跳过无意图条目", NO_INSTRUCTION_TEXT in p2 and "跳过" in p2)


def verify_manager_crud() -> None:
    print("==> 3. DiscussionManager CRUD（无 SDK）")
    from server.discussion_manager import DiscussionManager
    from server.discussions import Discussion, save_discussions
    from server.combined_summaries import save_combined_summaries, new_combined_summary
    from server.agents import AgentRecord

    class FakeManager:
        api_key = "test"
        agents = {
            "a1": AgentRecord(id="a1", name="A", cwd="/tmp", model="composer-2.5"),
        }

    with tempfile.TemporaryDirectory() as tmp:
        disc_path = Path(tmp) / "discussions.json"
        comb_path = Path(tmp) / "combined_summaries.json"
        d = Discussion(id="d1", agent_id="a1", anchor={"quote": "q"})
        with patch("server.discussions.discussions_file", return_value=disc_path):
            save_discussions({"d1": d})
        with patch("server.combined_summaries.combined_summaries_file", return_value=comb_path):
            save_combined_summaries({})

        with patch("server.discussions.discussions_file", return_value=disc_path), patch(
            "server.combined_summaries.combined_summaries_file", return_value=comb_path
        ):
            mgr = DiscussionManager(FakeManager())
            updated = mgr.update_discussion_summary("d1", "手动编辑")
            check("update_discussion_summary", updated and updated.summary == "手动编辑")

            combined = new_combined_summary("a1", ["d1"])
            combined.summary = "old"
            mgr._combined[combined.id] = combined
            save_combined_summaries(mgr._combined)
            cu = mgr.update_combined_summary(combined.id, "新合并")
            check("update_combined_summary", cu and cu.summary == "新合并")

            items = mgr.list_combined_for_agent("a1")
            check("list_combined_for_agent", len(items) == 1)

            deleted = mgr.delete_combined_summary(combined.id)
            check("delete_combined_summary", deleted is not None and not mgr._combined)

            mgr._combined[new_combined_summary("a1", ["d1"]).id] = new_combined_summary(
                "a1", ["d1"]
            )
            save_combined_summaries(mgr._combined)
            mgr._delete_combined_for_agent("a1")
            check("级联删除 agent 合并总结", len(mgr._combined) == 0)


def verify_websocket_handlers() -> None:
    print("==> 4. WebSocket 路由注册")
    app_py = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    required = [
        "summarize_discussion",
        "update_discussion_summary",
        "summarize_discussions",
        "list_combined_summaries",
        "update_combined_summary",
        "resummarize_combined",
        "delete_combined_summary",
        "run_summarize_discussion",
        "run_summarize_discussions",
    ]
    for key in required:
        check(f"app.py 含 {key}", key in app_py)


def verify_frontend() -> None:
    print("==> 5. 前端静态集成")
    app_js = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    style_css = (ROOT / "public" / "style.css").read_text(encoding="utf-8")

    checks = [
        ("discussion_summary_started", app_js),
        ("combined_summary_stream", app_js),
        ("injectSummaryToComposer", app_js),
        ("updateDiscussionSummaryDom", app_js),
        ("buildCombinedSummaryCard", app_js),
        ("list_combined_summaries", app_js),
        ("discuss-select-all", index_html),
        ("btn-summarize-selected", index_html),
        ("combined-summaries", index_html),
        (".discussion-summary", style_css),
        (".combined-summary-card", style_css),
    ]
    for key, src in checks:
        check(key, key in src)


def verify_two_phase_flow() -> None:
    print("==> 6. 两阶段合并逻辑（源码检查）")
    dm = (ROOT / "server" / "discussion_manager.py").read_text(encoding="utf-8")
    check("_ensure_discussion_summary 存在", "_ensure_discussion_summary" in dm)
    check("summarize_discussions 调用 ensure", "_ensure_discussion_summary" in dm)
    check("合并用 build_combined_summary_prompt", "build_combined_summary_prompt" in dm)
    check("combined_summary_progress 事件", "combined_summary_progress" in dm)
    check("regenerateIndividuals 支持", "regenerate_individuals" in dm)


def main() -> int:
    verify_models()
    verify_prompts()
    verify_manager_crud()
    verify_websocket_handlers()
    verify_frontend()
    verify_two_phase_flow()
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败:")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✅ 总结功能静态验证全部通过（未调用 Cursor API 做端到端流式测试）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
