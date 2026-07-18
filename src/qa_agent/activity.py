"""Human-readable activity events for the Web UI timeline.

Tool names and raw English tool dumps are translated into short Persian titles
plus a concrete detail / result line a non-engineer can follow.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any


INDEX_STEP_META: dict[str, dict[str, str]] = {
    "index_prepare": {"category": "index", "icon": "tool", "title": "آماده‌سازی فضای دانش"},
    "index_outline_sync": {"category": "index", "icon": "read", "title": "همگام‌سازی مستندات Outline"},
    "index_outline_doc": {"category": "index", "icon": "read", "title": "ذخیره سند Outline"},
    "index_github_clone": {"category": "index", "icon": "file", "title": "دریافت کد از GitHub"},
    "index_github_repo": {"category": "index", "icon": "file", "title": "به‌روزرسانی ریپازیتوری"},
    "index_confluence_sync": {"category": "index", "icon": "read", "title": "همگام‌سازی صفحات Confluence"},
    "index_confluence_page": {"category": "index", "icon": "read", "title": "ذخیره صفحه Confluence"},
    "index_azure_clone": {"category": "index", "icon": "file", "title": "دریافت کد از Azure DevOps"},
    "index_azure_repo": {"category": "index", "icon": "file", "title": "به‌روزرسانی ریپازیتوری Azure"},
    "index_openapi_sync": {"category": "index", "icon": "read", "title": "همگام‌سازی OpenAPI"},
    "index_openapi_spec": {"category": "index", "icon": "read", "title": "ذخیره spec API"},
    "index_corpus_map": {"category": "index", "icon": "search", "title": "ساخت ایندکس جستجو"},
    "index_graph_build": {"category": "index", "icon": "graph", "title": "ساخت گراف دانش"},
    "index_graph_skip": {"category": "index", "icon": "graph", "title": "گراف موقت (بدون build کامل)"},
    "index_finalize": {"category": "index", "icon": "write", "title": "جمع‌بندی ایندکس"},
}

TOOL_META: dict[str, dict[str, str]] = {
    "task": {"category": "agent", "icon": "delegate", "title": "واگذاری به متخصص"},
    "write_todos": {"category": "plan", "icon": "plan", "title": "برنامه‌ریزی مراحل"},
    "outline_search_titles": {"category": "outline", "icon": "search", "title": "جستجو در عناوین مستندات"},
    "outline_research_bundle": {"category": "outline", "icon": "search", "title": "تحقیق در مستندات"},
    "outline_get_document": {"category": "outline", "icon": "read", "title": "خواندن سند"},
    "corpus_search_docs": {"category": "outline", "icon": "search", "title": "جستجو در اسناد محلی"},
    "corpus_get_map": {"category": "outline", "icon": "read", "title": "خواندن نقشه مستندات"},
    "github_search_pull_requests": {"category": "github", "icon": "search", "title": "جستجوی Pull Request"},
    "github_discover_relevant_prs": {"category": "github", "icon": "search", "title": "پیدا کردن PRهای مرتبط"},
    "github_list_pull_requests": {"category": "github", "icon": "search", "title": "فهرست PRهای اخیر"},
    "github_get_pr_file_list": {"category": "github", "icon": "file", "title": "لیست فایل‌های تغییرکرده"},
    "github_get_pull_request": {"category": "github", "icon": "read", "title": "خواندن جزئیات PR"},
    "github_get_pr_changes": {"category": "github", "icon": "diff", "title": "بررسی تغییرات کد (diff)"},
    "github_get_file_content": {"category": "github", "icon": "file", "title": "خواندن فایل از ریپو"},
    "confluence_research_bundle": {"category": "confluence", "icon": "search", "title": "تحقیق در Confluence"},
    "confluence_get_page": {"category": "confluence", "icon": "read", "title": "خواندن صفحه Confluence"},
    "azure_discover_relevant_prs": {"category": "azure", "icon": "search", "title": "پیدا کردن PRهای Azure"},
    "azure_get_pull_request": {"category": "azure", "icon": "read", "title": "خواندن جزئیات PR (Azure)"},
    "azure_get_pr_changes": {"category": "azure", "icon": "diff", "title": "بررسی تغییرات PR (Azure)"},
    "azure_get_file_content": {"category": "azure", "icon": "file", "title": "خواندن فایل از Azure Repo"},
    "azure_search_work_items": {"category": "azure", "icon": "search", "title": "جستجوی Work Item (Boards)"},
    "azure_get_work_item": {"category": "azure", "icon": "read", "title": "خواندن Work Item"},
    "azure_wiki_search": {"category": "azure", "icon": "search", "title": "جستجو در Wiki (Azure)"},
    "azure_wiki_get_page": {"category": "azure", "icon": "read", "title": "خواندن صفحه Wiki (Azure)"},
    "openapi_research_bundle": {"category": "openapi", "icon": "search", "title": "تحقیق در OpenAPI"},
    "openapi_get_operation": {"category": "openapi", "icon": "read", "title": "خواندن قرارداد Endpoint"},
    "graphify_query": {"category": "graph", "icon": "graph", "title": "پرس‌وجو از گراف دانش"},
    "graphify_path": {"category": "graph", "icon": "graph", "title": "ردیابی ارتباط در گراف"},
    "write_feature_file": {"category": "output", "icon": "write", "title": "ذخیره فایل تست Gherkin"},
    # Built-in deepagents filesystem tools — shown with friendly Persian names.
    "ls": {"category": "tool", "icon": "file", "title": "مرور پوشه"},
    "glob": {"category": "tool", "icon": "search", "title": "پیدا کردن فایل‌ها"},
    "grep": {"category": "tool", "icon": "search", "title": "جستجو در متن فایل‌ها"},
    "read_file": {"category": "tool", "icon": "read", "title": "خواندن فایل"},
    "write_file": {"category": "tool", "icon": "write", "title": "نوشتن فایل"},
    "edit_file": {"category": "tool", "icon": "write", "title": "ویرایش فایل"},
    "execute": {"category": "tool", "icon": "tool", "title": "اجرای دستور"},
}

_SUBAGENT_LABELS = {
    "outline-researcher": "متخصص مستندات (Outline)",
    "github-researcher": "متخصص کد و PR (GitHub)",
    "confluence-researcher": "متخصص مستندات (Confluence)",
    "azure-researcher": "متخصص Azure DevOps (کد/Boards/Wiki)",
    "openapi-researcher": "متخصص قرارداد API (OpenAPI)",
}

_COMPLEXITY_LABELS = {
    "simple": "ساده",
    "standard": "معمولی",
    "complex": "پیچیده",
}

_ROLE_LABELS = {
    "generate": "مدل استاندارد",
    "pro": "مدل حرفه‌ای",
    "research": "مدل تحقیق",
    "nano": "مدل سبک",
}


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _quote(text: str, limit: int = 80) -> str:
    cleaned = _truncate(text, limit)
    return f"«{cleaned}»" if cleaned else ""


def _short_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    name = Path(raw).name
    if name and name != raw and len(raw) > 48:
        parent = Path(raw).parent.name
        return f"{parent}/{name}" if parent else name
    return raw if len(raw) <= 64 else "…" + raw[-60:]


def _repo_pr(repo: Any, number: Any) -> str:
    repo_s = str(repo or "").strip()
    num_s = str(number or "").strip()
    if repo_s and num_s:
        return f"{repo_s}#{num_s}"
    if num_s:
        return f"PR #{num_s}"
    return repo_s or "PR"


def _count_lines(text: str, prefix: str | None = None) -> int:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if prefix:
        return sum(1 for ln in lines if ln.strip().startswith(prefix))
    return len(lines)


def _extract_titles_from_read_next(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = re.search(r"READ NEXT:.*?\|([^|]+)\|", line)
        if match:
            titles.append(match.group(1).strip())
            continue
        match = re.search(r"-\s*\[([^\]]+)\]", line)
        if match:
            titles.append(match.group(1).strip())
    return titles


def _extract_pr_headline(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            return re.sub(r"^#+\s*", "", stripped)
        if stripped.startswith("PR ") or "#" in stripped[:40]:
            return stripped
    return ""


def _summarize_blocked(text: str) -> str:
    attempt = re.search(r"attempt\s+(\d+)\s*/\s*(\d+)", text, re.IGNORECASE)
    errors = [
        re.sub(r"^[-•]\s*", "", ln).strip()
        for ln in text.splitlines()
        if ln.strip().startswith(("-", "•"))
    ]
    head = "اعتبارسنجی رد شد"
    if attempt:
        head += f" (تلاش {attempt.group(1)} از {attempt.group(2)})"
    if not errors:
        return _truncate(f"{head} — {_truncate(text, 100)}", 160)
    first = errors[0]
    more = f" (+{len(errors) - 1} مورد دیگر)" if len(errors) > 1 else ""
    return _truncate(f"{head}: {first}{more}", 160)


def _format_tool_input(tool: str, data: dict[str, Any]) -> str:
    if tool == "task":
        raw = str(data.get("subagent_type") or data.get("name") or "subagent")
        label = _SUBAGENT_LABELS.get(raw, raw)
        desc = str(data.get("description") or data.get("prompt") or "").strip()
        if desc:
            return f"{label} — {_truncate(desc, 90)}"
        return label

    if tool == "write_todos":
        todos = data.get("todos") or []
        active = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"]
        if active:
            return f"الان: {_truncate(active[0], 100)}"
        pending = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") == "pending"]
        if pending:
            return f"بعدی: {_truncate(pending[0], 100)}"
        return f"{len(todos)} مرحله در برنامه"

    if tool in (
        "outline_search_titles",
        "outline_research_bundle",
        "corpus_search_docs",
        "github_search_pull_requests",
        "github_discover_relevant_prs",
        "confluence_research_bundle",
        "azure_discover_relevant_prs",
        "azure_search_work_items",
        "azure_wiki_search",
        "openapi_research_bundle",
        "graphify_query",
    ):
        q = data.get("query") or data.get("question") or ""
        return f"جستجو برای {_quote(str(q), 90)}" if q else ""

    if tool == "openapi_get_operation":
        return _truncate(str(data.get("operation") or ""), 80)

    if tool == "outline_get_document":
        title = data.get("title") or ""
        doc_id = str(data.get("document_id") or data.get("doc_id") or "")
        if title:
            return _quote(str(title), 100)
        if doc_id:
            return f"شناسه سند {doc_id[:10]}…"
        return ""

    if tool == "github_list_pull_requests":
        state = data.get("state") or "all"
        repo = data.get("repo") or ""
        state_fa = {"open": "باز", "closed": "بسته", "all": "همه"}.get(str(state), str(state))
        return f"{repo + ' — ' if repo else ''}وضعیت: {state_fa}"

    if tool in ("github_get_pr_file_list", "github_get_pull_request", "github_get_pr_changes"):
        label = {
            "github_get_pr_file_list": "فایل‌های",
            "github_get_pull_request": "جزئیات",
            "github_get_pr_changes": "تغییرات",
        }[tool]
        return f"{label} {_repo_pr(data.get('repo'), data.get('number'))}"

    if tool == "github_get_file_content":
        path = _short_path(str(data.get("path") or ""))
        repo = data.get("repo") or ""
        return f"{repo}: {path}" if repo and path else (path or str(repo))

    if tool == "graphify_path":
        src = data.get("source_concept") or data.get("source") or "?"
        tgt = data.get("target_concept") or data.get("target") or "?"
        return f"{src} ← → {tgt}"

    if tool == "write_feature_file":
        attempt = data.get("write_attempt") or 1
        query = str(data.get("query") or "").strip()
        bit = f"تلاش {attempt}"
        return f"{bit} — {_quote(query, 70)}" if query else bit

    if tool == "ls":
        return f"پوشه {_short_path(str(data.get('path') or '.'))}"

    if tool == "glob":
        pattern = data.get("pattern") or data.get("glob") or "*"
        return f"الگو: {pattern}"

    if tool == "grep":
        pattern = data.get("pattern") or data.get("query") or ""
        path = data.get("path") or data.get("file_path") or ""
        base = f"عبارت {_quote(str(pattern), 60)}"
        return f"{base} در {_short_path(str(path))}" if path else base

    if tool in ("read_file", "write_file", "edit_file"):
        path = data.get("file_path") or data.get("path") or data.get("filename") or ""
        return _short_path(str(path)) or "فایل"

    if tool == "execute":
        cmd = data.get("command") or data.get("cmd") or ""
        return _truncate(str(cmd), 100)

    # Generic fallback — avoid dumping raw key=value English noise.
    for key in ("query", "question", "path", "file_path", "name", "description"):
        if data.get(key):
            return _truncate(str(data[key]), 100)
    return ""


def _format_tool_output(tool: str, output: Any) -> str:
    text = ""
    if hasattr(output, "content"):
        text = str(getattr(output, "content", ""))
    elif isinstance(output, dict):
        text = str(output.get("content") or output.get("output") or output)
    else:
        text = str(output)

    text = text.strip()
    if not text:
        return "انجام شد"

    lower = text.lower()

    if tool == "task":
        if "error" in lower or "failed" in lower:
            return _truncate(f"خطا در ساب‌ایجنت: {_truncate(text, 100)}", 140)
        return _truncate(f"پاسخ متخصص آماده شد — {_truncate(text, 100)}", 140)

    if tool == "write_todos":
        return "برنامه به‌روز شد"

    if tool in ("outline_search_titles", "outline_research_bundle", "corpus_search_docs"):
        if (
            "no documents" in lower
            or "no corpus" in lower
            or "0 ranked" in lower
            or "not configured" in lower
        ):
            return "سندی پیدا نشد"
        titles = _extract_titles_from_read_next(text)
        if titles:
            shown = " · ".join(titles[:3])
            extra = f" (+{len(titles) - 3})" if len(titles) > 3 else ""
            return _truncate(f"{len(titles)} سند مرتبط: {shown}{extra}", 150)
        # Table rows as fallback count.
        rows = [ln for ln in text.splitlines() if "|" in ln and not re.match(r"^\s*\|?-+", ln)]
        if len(rows) >= 2:
            return f"{len(rows) - 1} نتیجه در جدول رتبه"
        return "جستجو انجام شد"

    if tool == "outline_get_document":
        words = len(text.split())
        return f"سند خوانده شد (~{words} کلمه)" if words > 20 else "سند خوانده شد"

    if tool == "corpus_get_map":
        return "نقشه مستندات آماده است" if "unavailable" not in lower else "نقشه مستندات موجود نیست"

    if tool in ("github_search_pull_requests", "github_list_pull_requests"):
        if "no pull requests" in lower or "not configured" in lower:
            return "PRای پیدا نشد"
        count = sum(1 for ln in text.splitlines() if re.search(r"\bPR\b|#\d+", ln))
        return f"{count} مورد PR پیدا شد" if count else "لیست PR دریافت شد"

    if tool == "github_discover_relevant_prs":
        if "no relevant" in lower or "not configured" in lower:
            return "PR مرتبطی پیدا نشد"
        headline = _extract_pr_headline(text)
        if headline:
            clean = re.sub(r"^Top:\s*", "", headline)
            clean = re.sub(r"\s*\(score=\d+\)\s*$", "", clean)
            return _truncate(f"مرتبط‌ترین: {clean}", 140)
        return "PRهای مرتبط رتبه‌بندی شدند"

    if tool == "github_get_pr_file_list":
        count = sum(
            1
            for ln in text.splitlines()
            if ln.startswith("  ") and "[" in ln and "]" in ln
        )
        if count:
            return f"{count} فایل تغییر کرده"
        return "لیست فایل‌ها دریافت شد"

    if tool == "github_get_pull_request":
        title_match = re.search(r"(?im)^(?:title|عنوان)\s*:\s*(.+)$", text)
        if title_match:
            return _truncate(f"عنوان: {title_match.group(1).strip()}", 140)
        return "جزئیات PR دریافت شد"

    if tool == "github_get_pr_changes":
        files = sum(1 for ln in text.splitlines() if re.match(r"^\s*\[[A-Z]\]\s+", ln))
        added = len(re.findall(r"(?m)^\+", text))
        removed = len(re.findall(r"(?m)^-", text))
        bits = []
        if files:
            bits.append(f"{files} فایل")
        if added or removed:
            bits.append(f"+{added}/−{removed} خط")
        return "diff بررسی شد" + (f" — {' · '.join(bits)}" if bits else "")

    if tool == "github_get_file_content":
        lines = _count_lines(text)
        return f"فایل خوانده شد ({lines} خط)" if lines else "فایل خوانده شد"

    if tool == "graphify_query":
        if "not built" in lower or "unavailable" in lower or "no graph" in lower:
            return "گراف هنوز ساخته نشده"
        if "no graph nodes matched" in lower or "no nodes" in lower:
            return "نتیجه‌ای در گراف نبود"
        return _truncate(f"پاسخ گراف: {_truncate(text, 110)}", 150)

    if tool == "graphify_path":
        if "unavailable" in lower or "not built" in lower:
            return "مسیر در گراف در دسترس نیست"
        return "مسیر ارتباط پیدا شد" if text else "مسیری پیدا نشد"

    if tool == "write_feature_file":
        if text.startswith("BLOCKED") or "BLOCKED" in text[:40]:
            return _summarize_blocked(text)
        if "Written:" in text or "written:" in lower:
            coverage = re.search(r"Coverage:\s*(\d+)/(\d+)", text)
            warn = re.search(r"Quality warnings\s*\((\d+)\)", text)
            bits = ["فایل .feature ذخیره شد"]
            if coverage:
                bits.append(f"پوشش {coverage.group(1)} از {coverage.group(2)}")
            if warn:
                bits.append(f"{warn.group(1)} هشدار کیفیت")
            return " — ".join(bits)
        return _truncate(text, 140)

    if tool == "ls":
        entries = [ln for ln in text.splitlines() if ln.strip()]
        return f"{len(entries)} مورد در پوشه" if entries else "پوشه خالی است"

    if tool == "glob":
        entries = [ln for ln in text.splitlines() if ln.strip()]
        return f"{len(entries)} فایل پیدا شد" if entries else "فایلی پیدا نشد"

    if tool == "grep":
        if "no matches" in lower or not text.strip():
            return "موردی پیدا نشد"
        hits = _count_lines(text)
        return f"{hits} تطبیق پیدا شد"

    if tool == "read_file":
        return f"خوانده شد ({_count_lines(text)} خط)"

    if tool in ("write_file", "edit_file"):
        return "فایل ذخیره شد"

    if tool == "execute":
        if "error" in lower or "traceback" in lower:
            return _truncate(f"خطا در اجرا: {_truncate(text, 100)}", 140)
        return "دستور اجرا شد"

    # Last resort: never dump huge English blobs.
    if len(text) > 180 or text.count("\n") > 4:
        return f"خروجی آماده شد ({_count_lines(text)} خط)"
    return _truncate(text, 140)


def make_activity(
    *,
    tool: str,
    phase: str,
    detail: str = "",
    result_preview: str = "",
    status: str = "running",
    title: str | None = None,
    category: str | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    meta = TOOL_META.get(tool) or INDEX_STEP_META.get(tool) or {
        "category": "tool",
        "icon": "tool",
        "title": tool.replace("_", " "),
    }
    return {
        "id": uuid.uuid4().hex[:10],
        "tool": tool,
        "category": category or meta["category"],
        "icon": icon or meta["icon"],
        "title": title or meta["title"],
        "detail": detail,
        "result_preview": result_preview,
        "status": status,
        "phase": phase,
        "timestamp": time.time(),
    }


def activity_from_tool_start(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    detail = _format_tool_input(tool, tool_input if isinstance(tool_input, dict) else {})
    return make_activity(tool=tool, phase="start", detail=detail, status="running")


def activity_from_tool_end(tool: str, tool_input: dict[str, Any], output: Any) -> dict[str, Any]:
    detail = _format_tool_input(tool, tool_input if isinstance(tool_input, dict) else {})
    preview = _format_tool_output(tool, output)
    return make_activity(
        tool=tool,
        phase="end",
        detail=detail,
        result_preview=preview,
        status="done",
    )


def activity_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize non-tool events (triage, escalation, …) into timeline cards."""
    kind = event.get("type") or event.get("tool")
    if kind == "partial_gherkin":
        return None
    if kind == "triage":
        complexity = str(event.get("complexity") or "standard")
        role = str(event.get("generate_role") or "generate")
        reason = str(event.get("reason") or "").strip()
        source = str(event.get("source") or "")
        detail = (
            f"سطح {_COMPLEXITY_LABELS.get(complexity, complexity)} → "
            f"{_ROLE_LABELS.get(role, role)}"
        )
        if reason:
            detail += f" — {_truncate(reason, 90)}"
        preview = {
            "llm": "تصمیم مدل",
            "heuristic": "تخمین ساده",
            "disabled": "غیرفعال",
        }.get(source, source)
        return make_activity(
            tool="triage",
            phase="triage",
            detail=detail,
            result_preview=preview or "انجام شد",
            status="done",
            title="ارزیابی پیچیدگی درخواست",
            category="plan",
            icon="plan",
        )
    if kind == "escalation":
        return make_activity(
            tool="escalation",
            phase="generate",
            detail=str(event.get("detail") or "نوشتن بلاک شد — تلاش با مدل حرفه‌ای"),
            result_preview="سوییچ به مدل Pro",
            status="done",
            title="ارتقای مدل (Escalation)",
            category="agent",
            icon="delegate",
        )
    # Already a full activity card (index steps, etc.).
    if event.get("title") and event.get("tool"):
        return event
    return None


def phase_for_tool(tool: str) -> str:
    meta = TOOL_META.get(tool) or INDEX_STEP_META.get(tool, {})
    category = meta.get("category", "tool")
    mapping = {
        "plan": "planning",
        "agent": "delegating",
        "outline": "research",
        "github": "research",
        "confluence": "research",
        "azure": "research",
        "openapi": "research",
        "graph": "graph",
        "output": "writing",
        "index": "indexing",
        "tool": "running",
    }
    return mapping.get(category, "running")


def make_index_activity(
    step: str,
    *,
    detail: str = "",
    result_preview: str = "",
    status: str = "running",
) -> dict[str, Any]:
    meta = INDEX_STEP_META.get(step, {"category": "index", "icon": "tool", "title": step})
    return make_activity(
        tool=step,
        phase=phase_for_tool(step),
        detail=detail,
        result_preview=result_preview,
        status=status,
        title=meta["title"],
        category=meta["category"],
        icon=meta["icon"],
    )
