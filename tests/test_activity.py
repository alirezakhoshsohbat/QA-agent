"""Readable activity log formatting."""

from __future__ import annotations

from qa_agent.activity import (
    activity_from_event,
    activity_from_tool_end,
    activity_from_tool_start,
)


def test_task_uses_persian_subagent_label():
    start = activity_from_tool_start(
        "task",
        {"subagent_type": "outline-researcher", "description": "Find login docs"},
    )
    assert start["title"] == "واگذاری به متخصص"
    assert "متخصص مستندات" in start["detail"]
    assert "outline-researcher" not in start["title"]


def test_outline_bundle_summarizes_docs_not_raw_dump():
    raw = (
        "- READ NEXT: id=abc | Search Pane PRD | layer=frontend\n"
        "- READ NEXT: id=def | BFF Contract | layer=backend\n"
    )
    end = activity_from_tool_end("outline_research_bundle", {"query": "search"}, raw)
    assert "سند مرتبط" in end["result_preview"]
    assert "READ NEXT" not in end["result_preview"]
    assert "جستجو برای" in end["detail"]


def test_blocked_write_is_persian_summary():
    raw = (
        "BLOCKED (attempt 1/4):\n"
        "- Missing acceptance tags for checklist IDs: E-1, E-2\n"
        "- Scenario 'X' is missing Given/When/Then steps\n"
    )
    end = activity_from_tool_end("write_feature_file", {"query": "x", "write_attempt": 1}, raw)
    assert "اعتبارسنجی رد شد" in end["result_preview"]
    assert "تلاش 1 از 4" in end["result_preview"]
    assert "BLOCKED" not in end["result_preview"]


def test_filesystem_tools_have_friendly_titles():
    start = activity_from_tool_start("read_file", {"file_path": "/tmp/corpus/docs/foo.md"})
    assert start["title"] == "خواندن فایل"
    assert "foo.md" in start["detail"]
    assert "file_path=" not in start["detail"]


def test_triage_event_becomes_timeline_card():
    card = activity_from_event(
        {
            "type": "triage",
            "complexity": "complex",
            "generate_role": "pro",
            "reason": "max coverage across features",
            "source": "llm",
        }
    )
    assert card is not None
    assert card["title"] == "ارزیابی پیچیدگی درخواست"
    assert "پیچیده" in card["detail"]
    assert "حرفه‌ای" in card["detail"]
    assert card["status"] == "done"


def test_github_diff_summarizes_instead_of_dumping_patch():
    raw = "### files\n  [M] a.ts\n  [A] b.ts\n+++ a\n+line1\n+line2\n-old\n"
    end = activity_from_tool_end(
        "github_get_pr_changes",
        {"repo": "org/web", "number": 12},
        raw,
    )
    assert "diff بررسی شد" in end["result_preview"]
    assert "@@" not in end["result_preview"]
    assert "تغییرات org/web#12" in end["detail"]
