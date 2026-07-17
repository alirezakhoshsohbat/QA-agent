from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from qa_agent.activity import make_index_activity
from qa_agent.config import Settings, get_settings
from qa_agent.tools.graphify import build_graph
from qa_agent.tools.github import GitHubClient
from qa_agent.tools.outline import OutlineClient

IndexActivityCallback = Callable[[dict[str, Any]], None]


def _safe_filename(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:80] or "document"


def _emit_start(on_activity: IndexActivityCallback | None, step: str, detail: str = "") -> None:
    if on_activity:
        on_activity(make_index_activity(step, detail=detail, status="running"))


def _emit_end(
    on_activity: IndexActivityCallback | None,
    step: str,
    *,
    detail: str = "",
    result_preview: str = "",
) -> None:
    if on_activity:
        on_activity(
            make_index_activity(
                step,
                detail=detail,
                result_preview=result_preview,
                status="done",
            )
        )


def sync_outline_docs(
    settings: Settings | None = None,
    on_activity: IndexActivityCallback | None = None,
) -> int:
    """Export Outline documents to corpus/docs/ as markdown files."""
    settings = settings or get_settings()
    client = OutlineClient(settings)
    docs_dir = settings.corpus_docs_dir
    docs_dir.mkdir(parents=True, exist_ok=True)

    if not settings.outline_api_key:
        _emit_start(on_activity, "index_outline_sync", "Outline API key تنظیم نشده")
        _emit_end(on_activity, "index_outline_sync", result_preview="رد شد — API key موجود نیست")
        return 0

    _emit_start(on_activity, "index_outline_sync", "دریافت لیست اسناد از Outline")

    try:
        with httpx.Client(timeout=30.0) as http:
            response = http.post(
                f"{settings.outline_base_url.rstrip('/')}/documents.list",
                headers={
                    "Authorization": f"Bearer {settings.outline_api_key}",
                    "Content-Type": "application/json",
                },
                json={"limit": 100},
            )
            response.raise_for_status()
            documents = response.json().get("data", [])
    except httpx.HTTPError:
        documents = []
        for term in ["", "guide", "api", "feature", "test"]:
            results = client.search_titles(term, limit=50)
            for item in results:
                doc = item.get("document") or item
                if doc.get("id"):
                    documents.append(doc)

    seen: set[str] = set()
    unique_docs: list[dict] = []
    for doc in documents:
        doc_id = doc.get("id", "")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        unique_docs.append(doc)

    count = 0
    for doc in unique_docs:
        doc_id = doc.get("id", "")
        title = doc.get("title", doc_id)
        _emit_start(on_activity, "index_outline_doc", title)
        try:
            text = client.export_document_text(doc_id)
            path = docs_dir / f"{_safe_filename(title)}-{doc_id[:8]}.md"
            path.write_text(text, encoding="utf-8")
            count += 1
            _emit_end(on_activity, "index_outline_doc", detail=title, result_preview=f"ذخیره شد — {path.name}")
        except httpx.HTTPError:
            _emit_end(on_activity, "index_outline_doc", detail=title, result_preview="خطا در export")

    _emit_end(
        on_activity,
        "index_outline_sync",
        result_preview=f"{count} سند export شد",
    )
    return count


def sync_github_code(
    settings: Settings | None = None,
    on_activity: IndexActivityCallback | None = None,
) -> list[str]:
    """Clone or pull all configured GitHub repos into corpus/code/{slug}/."""
    settings = settings or get_settings()
    code_dir = settings.corpus_code_dir
    repos = settings.github_repos_list

    if not repos:
        code_dir.mkdir(parents=True, exist_ok=True)
        _emit_start(on_activity, "index_github_clone", "repoای در تنظیمات نیست")
        _emit_end(on_activity, "index_github_clone", result_preview=f"فقط پوشه {code_dir.name} آماده شد")
        return [str(code_dir)]

    _emit_start(on_activity, "index_github_clone", f"{len(repos)} repo")

    client = GitHubClient(settings)
    paths: list[str] = []
    errors: list[str] = []
    for repo in repos:
        _emit_start(on_activity, "index_github_repo", repo)
        slug = repo.split("/")[-1] if "/" in repo else repo
        try:
            path, action = client.clone_configured_repo(repo, code_dir)
            paths.append(str(path))
            _emit_end(
                on_activity,
                "index_github_repo",
                detail=repo,
                result_preview=f"{action} — {slug}/",
            )
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            message = str(exc).strip() or exc.__class__.__name__
            errors.append(f"{repo}: {message[:160]}")
            _emit_end(
                on_activity,
                "index_github_repo",
                detail=repo,
                result_preview=f"خطا — {message[:120]}",
            )

    preview = f"{len(paths)} repo آماده"
    if errors:
        preview += f" · {len(errors)} خطا"
    _emit_end(on_activity, "index_github_clone", result_preview=preview)
    return paths


def run_index(
    outline_sync: bool = False,
    github_clone: bool = False,
    with_graph: bool = False,
    settings: Settings | None = None,
    on_activity: IndexActivityCallback | None = None,
) -> dict:
    settings = settings or get_settings()

    result: dict = {"docs_synced": 0, "code_dirs": [], "graph_built": False, "graph_skipped": not with_graph}

    _emit_start(on_activity, "index_prepare", str(settings.qa_agent_corpus_dir))
    settings.qa_agent_corpus_dir.mkdir(parents=True, exist_ok=True)
    settings.qa_agent_graphify_out.mkdir(parents=True, exist_ok=True)
    _emit_end(on_activity, "index_prepare", result_preview="پوشه‌های corpus آماده")

    if outline_sync:
        result["docs_synced"] = sync_outline_docs(settings, on_activity=on_activity)
    else:
        _emit_start(on_activity, "index_outline_sync", "غیرفعال")
        _emit_end(on_activity, "index_outline_sync", result_preview="رد شد")

    if github_clone:
        result["code_dirs"] = sync_github_code(settings, on_activity=on_activity)
    else:
        _emit_start(on_activity, "index_github_clone", "غیرفعال")
        _emit_end(on_activity, "index_github_clone", result_preview="رد شد")

    # Retrieval index + corpus map are cheap and deterministic — always refresh.
    _emit_start(on_activity, "index_corpus_map", "BM25 index + corpus map")
    try:
        from qa_agent.corpus_map import build_corpus_map
        from qa_agent.retrieval import EmbeddingClient, get_corpus_index

        embedder = EmbeddingClient(settings)
        doc_count = get_corpus_index(settings).build(embedder if embedder.enabled else None)
        map_path = build_corpus_map(settings)
        result["corpus_map"] = str(map_path)
        result["docs_indexed"] = doc_count
        preview = f"{doc_count} سند ایندکس شد"
        if embedder.enabled:
            preview += " (+embeddings)"
        _emit_end(on_activity, "index_corpus_map", result_preview=preview)
    except Exception as exc:  # retrieval index must never break corpus indexing
        _emit_end(on_activity, "index_corpus_map", result_preview=f"خطا — {str(exc)[:120]}")

    if with_graph:
        _emit_start(on_activity, "index_graph_build", "graphify build (کند)")
        build_graph(settings.qa_agent_corpus_dir, settings.qa_agent_graphify_out, update=True)
        result["graph_built"] = (settings.qa_agent_graphify_out / "graph.json").exists()
        result["graph_skipped"] = False
        graph_path = settings.qa_agent_graphify_out / "graph.json"
        node_count = 0
        if graph_path.exists():
            import json

            try:
                node_count = len(json.loads(graph_path.read_text(encoding="utf-8")).get("nodes", []))
            except (json.JSONDecodeError, OSError):
                node_count = 0
        _emit_end(
            on_activity,
            "index_graph_build",
            result_preview=f"graph.json — {node_count} node",
        )
    else:
        from qa_agent.tools.graphify import graph_exists
        import json

        _emit_start(on_activity, "index_graph_skip", "fast mode — placeholder graph")
        if not graph_exists(settings.qa_agent_graphify_out):
            placeholder = {
                "nodes": [],
                "edges": [],
                "note": "Graph skipped for speed. Run: qa-agent index --with-graph",
            }
            (settings.qa_agent_graphify_out / "graph.json").write_text(
                json.dumps(placeholder, indent=2), encoding="utf-8"
            )
        result["graph_built"] = True
        _emit_end(on_activity, "index_graph_skip", result_preview="placeholder graph.json")

    _emit_start(on_activity, "index_finalize", "جمع‌بندی نتیجه")
    summary = []
    if outline_sync:
        summary.append(f"{result['docs_synced']} doc")
    if github_clone:
        summary.append(f"{len(result['code_dirs'])} repo")
    summary.append("graph ✓" if result["graph_built"] else "graph ✗")
    _emit_end(on_activity, "index_finalize", result_preview=" · ".join(summary))

    return result
