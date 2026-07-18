"""Confluence (Atlassian Cloud) documentation research source.

Mirrors the Outline connector: a thin REST client plus research tools the
QA agent uses to find and read documentation, build the acceptance checklist,
and gather wire-level facts. Auth is HTTP Basic with the account email and an
API token. When Confluence is not configured (or a call fails) the tools fall
back to the locally indexed corpus so a run never hard-fails on connectivity.
"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx
from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker
from qa_agent.models.schemas import FileCache
from qa_agent.query_expansion import expand_query_terms
from qa_agent.retrieval import SEARCH_STOP_WORDS
from qa_agent.tools.outline import _layer_hint, search_corpus_docs

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence 'storage' (XHTML) body to readable plain text."""
    if not storage_html:
        return ""
    text = re.sub(r"</(p|h[1-6]|li|tr|div|table)>", "\n", storage_html, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _confluence_search_terms(query: str, settings: Settings | None = None) -> list[str]:
    """Derive useful CQL text terms from an arbitrary (possibly Persian) query."""
    terms: list[str] = []
    terms.extend(expand_query_terms(query, settings))

    tokens = re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
    keywords = [t for t in tokens if len(t) >= 3 and t not in SEARCH_STOP_WORDS]
    keywords = list(dict.fromkeys(keywords))
    if len(keywords) >= 2:
        terms.append(" ".join(keywords[:3]))
    terms.extend(keywords[:4])
    if not terms:
        terms.append(query.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term.strip())
    return deduped[:6]


class ConfluenceClient:
    """Minimal Atlassian Confluence Cloud REST client (v1 content API)."""

    def __init__(self, settings: Settings | None = None, cost: CostTracker | None = None) -> None:
        self.settings = settings or get_settings()
        self.cost = cost
        self.cache = FileCache(self.settings.cache_dir)

    @property
    def configured(self) -> bool:
        return self.settings.confluence_configured

    def _auth(self) -> tuple[str, str]:
        return (self.settings.confluence_email, self.settings.confluence_api_token)

    def _uses_bearer(self) -> bool:
        return (self.settings.confluence_auth_mode or "basic").strip().lower() == "bearer"

    def _request_kwargs(self) -> dict[str, Any]:
        """Auth headers/creds for the configured scheme.

        Cloud uses HTTP Basic (email + API token); on-prem Server/Data Center
        uses a Bearer Personal Access Token.
        """
        headers = {"Accept": "application/json"}
        if self._uses_bearer():
            headers["Authorization"] = f"Bearer {self.settings.confluence_api_token}"
            return {"headers": headers}
        return {"headers": headers, "auth": self._auth()}

    def _api_root(self) -> str:
        return f"{self.settings.confluence_base_url.rstrip('/')}/rest/api"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.cost:
            self.cost.record_confluence_call()
        url = f"{self._api_root()}/{path.lstrip('/')}"
        with httpx.Client(timeout=30.0, verify=self.settings.httpx_verify) as client:
            response = client.get(url, params=params or {}, **self._request_kwargs())
            response.raise_for_status()
            return response.json()

    def _space_clause(self) -> str:
        spaces = self.settings.confluence_spaces_list
        if not spaces:
            return ""
        joined = ",".join(f'"{s}"' for s in spaces)
        return f" and space in ({joined})"

    def search(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        cache_key = f"search_{text}_{limit}"
        cached = self.cache.get("confluence", cache_key, ttl_seconds=86400)
        if cached is not None:
            return cached

        safe = text.replace('"', " ").strip()
        cql = f'type=page and text ~ "{safe}"{self._space_clause()}'
        try:
            result = self._get("content/search", {"cql": cql, "limit": limit})
        except httpx.HTTPStatusError:
            # Retry without the space clause — an invalid space key otherwise 400s.
            result = self._get(
                "content/search",
                {"cql": f'type=page and text ~ "{safe}"', "limit": limit},
            )
        pages = result.get("results", [])
        self.cache.set("confluence", cache_key, pages)
        return pages

    def get_page(self, page_id: str) -> dict[str, Any]:
        cached = self.cache.get("confluence", page_id, ttl_seconds=86400)
        if cached is not None:
            return cached
        result = self._get(
            f"content/{page_id}",
            {"expand": "body.storage,space,version"},
        )
        self.cache.set("confluence", page_id, result)
        return result

    def export_page_text(self, page_id: str) -> str:
        page = self.get_page(page_id)
        title = page.get("title", "Untitled")
        body = (page.get("body") or {}).get("storage", {}).get("value", "")
        return f"# {title}\n\n{_storage_to_text(body)}"


def _normalize_page(raw: dict[str, Any]) -> dict[str, str]:
    page_id = str(raw.get("id") or "")
    title = str(raw.get("title") or "Untitled")
    excerpt = raw.get("excerpt") or ""
    body = (raw.get("body") or {}).get("storage", {}).get("value", "")
    snippet = _storage_to_text(str(excerpt or body))[:240]
    return {"id": page_id, "title": title, "snippet": snippet}


def research_confluence_pages(
    client: ConfluenceClient,
    query: str,
    *,
    max_results: int = 8,
) -> list[dict[str, str]]:
    ranked: dict[str, dict[str, str]] = {}
    for term in _confluence_search_terms(query, client.settings):
        try:
            pages = client.search(term, limit=max_results)
        except httpx.HTTPError:
            continue
        for raw in pages:
            entry = _normalize_page(raw)
            if not entry["id"] or entry["id"] in ranked:
                continue
            entry["layer"] = _layer_hint(entry["title"], entry["snippet"])
            entry["matched_term"] = term
            ranked[entry["id"]] = entry
    return list(ranked.values())[:max_results]


def create_confluence_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
    max_docs: int | None = None,
) -> list:
    client = ConfluenceClient(settings, cost)
    cfg = settings or get_settings()
    limit = max_docs or cfg.qa_agent_max_docs
    read_ids: set[str] = set()

    def _corpus_fallback(query: str, max_results: int, header: str) -> str:
        hits = search_corpus_docs(query, cfg.corpus_docs_dir, max_results=max_results, settings=cfg)
        if not hits:
            return "Confluence not configured and no local corpus matches."
        lines = [header]
        for hit in hits:
            lines.append(f"- [{hit['title']}] id={hit['id']} layer={hit['layer']} score={hit['score']}")
        return "\n".join(lines)

    @tool
    def confluence_research_bundle(query: str, max_results: int = 8) -> str:
        """One-shot Confluence research: tries multiple search terms, ranks pages.

        Prefer this FIRST for documentation research. Returns page IDs, titles,
        layer hints, and the matched term. Read the top few via confluence_get_page.
        """
        if not client.configured:
            return _corpus_fallback(query, max_results, "Local corpus matches (Confluence unavailable):")

        pages = research_confluence_pages(client, query, max_results=min(max_results, limit))
        if not pages:
            fallback = _corpus_fallback(query, max_results, "No Confluence hits. Local corpus matches:")
            return fallback if "corpus" in fallback else "No pages found. Try broader terms."

        lines = [
            f"Found {len(pages)} Confluence pages for query: {query}",
            "",
            "| ID | Title | Layer | Matched term |",
            "|----|-------|-------|--------------|",
        ]
        for page in pages:
            lines.append(f"| {page['id']} | {page['title']} | {page['layer']} | {page['matched_term']} |")
        lines.extend(["", "Read up to 3 distinct page IDs via confluence_get_page (one per layer)."])
        for page in pages[: min(3, limit)]:
            lines.append(f"- READ NEXT: id={page['id']} | {page['title']} | layer={page['layer']}")
        return "\n".join(lines)

    @tool
    def confluence_get_page(page_id: str) -> str:
        """Fetch a Confluence page's text by ID. Call once per distinct ID from the bundle."""
        pid = page_id.strip()
        if pid in read_ids:
            return f"Page {pid} was already read this run. Pick a different ID — do not re-fetch."
        read_ids.add(pid)
        try:
            text = client.export_page_text(pid)
        except httpx.HTTPStatusError as exc:
            return (
                f"Could not fetch Confluence page '{pid}': HTTP {exc.response.status_code}. "
                "Do NOT retry this ID — pick a different page and continue writing scenarios."
            )
        except httpx.HTTPError as exc:
            return (
                f"Could not fetch Confluence page '{pid}': {type(exc).__name__}. "
                "Do NOT retry this ID — continue with the pages you have."
            )
        if cost:
            cost.add_source(f"confluence:{pid}")
        max_chars = 6000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars for token budget]"
        return text

    return [confluence_research_bundle, confluence_get_page]
