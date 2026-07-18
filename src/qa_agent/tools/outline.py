from __future__ import annotations

import json
import re
from typing import Any

import httpx
from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker
from qa_agent.domain import load_domain_rules
from qa_agent.models.schemas import FileCache
from qa_agent.query_expansion import expand_query_terms
from qa_agent.retrieval import SEARCH_STOP_WORDS, hybrid_search


class OutlineClient:
    """RPC-style Outline API client."""

    def __init__(self, settings: Settings | None = None, cost: CostTracker | None = None) -> None:
        self.settings = settings or get_settings()
        self.cost = cost
        self.cache = FileCache(self.settings.cache_dir)
        self._headers = {
            "Authorization": f"Bearer {self.settings.outline_api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.cost:
            self.cost.record_outline_call()
        url = f"{self.settings.outline_base_url.rstrip('/')}/{method}"
        with httpx.Client(timeout=30.0, verify=self.settings.httpx_verify) as client:
            response = client.post(url, headers=self._headers, json=payload)
            response.raise_for_status()
            return response.json()

    def search_titles(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        cache_key = f"titles_{query}_{limit}"
        cached = self.cache.get("outline", cache_key, ttl_seconds=86400)
        if cached is not None:
            return cached

        result = self._post("documents.search_titles", {"query": query, "limit": limit})
        documents = result.get("data", [])
        self.cache.set("outline", cache_key, documents)
        return documents

    def search_documents(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        cache_key = f"search_{query}_{limit}"
        cached = self.cache.get("outline", cache_key, ttl_seconds=86400)
        if cached is not None:
            return cached

        result = self._post("documents.search", {"query": query, "limit": limit})
        documents = result.get("data", [])
        self.cache.set("outline", cache_key, documents)
        return documents

    def get_document(self, document_id: str) -> dict[str, Any]:
        cached = self.cache.get("outline", document_id, ttl_seconds=86400)
        if cached is not None:
            return cached

        result = self._post("documents.info", {"id": document_id})
        document = result.get("data", {})
        self.cache.set("outline", document_id, document)
        return document

    def export_document_text(self, document_id: str) -> str:
        doc = self.get_document(document_id)
        title = doc.get("title", "Untitled")
        text = doc.get("text", "") or doc.get("content", "") or ""
        if not text and doc.get("id"):
            try:
                result = self._post("documents.export", {"id": document_id})
                text = result.get("data", "")
            except httpx.HTTPError:
                text = json.dumps(doc, ensure_ascii=False)
        return f"# {title}\n\n{text}"


def _normalize_doc_entry(raw: dict[str, Any]) -> dict[str, str]:
    nested = raw.get("document") or {}
    doc_id = str(nested.get("id") or raw.get("id") or "")
    title = str(nested.get("title") or raw.get("title") or "Untitled")
    snippet = str(raw.get("context") or raw.get("snippet") or raw.get("text") or "")[:240]
    return {"id": doc_id, "title": title, "snippet": snippet}


def _layer_hint(title: str, snippet: str) -> str:
    """Classify a doc's layer from title+snippet using the domain profile's
    ordered layer hints; falls back to 'unknown'."""
    combined = f"{title} {snippet}".lower()
    for layer, terms in load_domain_rules().layer_hints:
        if any(term in combined for term in terms):
            return layer
    return "unknown"


def _leading_query_phrase(query_lower: str) -> str | None:
    """The first two meaningful (non-stopword) query tokens joined, e.g.
    'search pane' or 'login flow' — used for a generic title-match boost."""
    tokens = [
        token
        for token in re.findall(r"[^\W_]+", query_lower, flags=re.UNICODE)
        if len(token) >= 3 and token not in _SEARCH_STOP_WORDS
    ]
    tokens = list(dict.fromkeys(tokens))
    return " ".join(tokens[:2]) if len(tokens) >= 2 else None


def _score_doc(query: str, title: str, snippet: str) -> int:
    query_lower = query.lower()
    title_lower = title.lower()
    snippet_lower = snippet.lower()
    score = 0
    for token in re.findall(r"[\w-]+", query_lower):
        if len(token) < 3:
            continue
        if token in title_lower:
            score += 8
        if token in snippet_lower:
            score += 4
    phrase = _leading_query_phrase(query_lower)
    if phrase and phrase in title_lower:
        score += 15
    if "acceptance" in snippet_lower:
        score += 6
    if "prd" in title_lower:
        score += 3
    return score


# Canonical stopword set lives in qa_agent.retrieval; kept under the old name
# because scoring helpers and tests reference it.
_SEARCH_STOP_WORDS = SEARCH_STOP_WORDS


def _derive_search_terms(query: str) -> list[str]:
    """Build useful Outline search terms from an arbitrary (possibly Persian) query.

    Produces the meaningful phrase, kebab-case variants, and individual keywords
    so short title searches and full-text searches both have a chance to hit.
    """
    tokens = re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
    keywords = [t for t in tokens if len(t) >= 3 and t not in _SEARCH_STOP_WORDS]
    keywords = list(dict.fromkeys(keywords))

    terms: list[str] = []
    if len(keywords) >= 2:
        phrase = " ".join(keywords[:3])
        terms.append(phrase)
        terms.append(phrase.replace(" ", "-"))
    terms.extend(keywords[:4])
    return terms


def _research_search_terms(query: str, settings: Settings | None = None) -> list[str]:
    normalized = query.lower().strip()
    terms: list[str] = []

    for key, aliases in load_domain_rules().outline_search_aliases.items():
        if key in normalized:
            terms.extend(aliases)
            break

    # LLM query understanding: Persian/vague request → English wiki terms.
    # Cached per query; returns [] when disabled/unavailable, so the heuristic
    # keyword derivation below still guarantees search terms.
    terms.extend(expand_query_terms(query, settings))

    if not terms:
        terms.extend(_derive_search_terms(query))
        if not terms:
            terms.append(query.strip())

    # Short single-token fallbacks often work better than long phrases in Outline title search.
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped[:8]


def research_outline_documents(
    client: OutlineClient,
    query: str,
    *,
    max_results: int = 8,
) -> list[dict[str, str]]:
    ranked: dict[str, dict[str, str | int]] = {}

    search_terms = _research_search_terms(query, client.settings)
    # Score against query + expanded terms so English docs found for a Persian
    # request still rank by relevance instead of falling to zero.
    scoring_query = f"{query} {' '.join(search_terms)}"

    for term in search_terms:
        for doc in client.search_titles(term, limit=max_results):
            entry = _normalize_doc_entry(doc)
            if not entry["id"]:
                continue
            score = _score_doc(scoring_query, entry["title"], entry["snippet"])
            existing = ranked.get(entry["id"])
            if not existing or score > int(existing["score"]):
                ranked[entry["id"]] = {
                    **entry,
                    "score": score,
                    "matched_term": term,
                    "layer": _layer_hint(entry["title"], entry["snippet"]),
                }

        # Full-text search catches BFF docs title search misses (e.g. "search-pane-sync").
        for doc in client.search_documents(term, limit=max_results):
            entry = _normalize_doc_entry(doc)
            if not entry["id"]:
                continue
            score = _score_doc(scoring_query, entry["title"], entry["snippet"]) + 2
            existing = ranked.get(entry["id"])
            if not existing or score > int(existing["score"]):
                ranked[entry["id"]] = {
                    **entry,
                    "score": score,
                    "matched_term": term,
                    "layer": _layer_hint(entry["title"], entry["snippet"]),
                }

    query_lower = query.lower()
    for key, doc_ids in load_domain_rules().outline_doc_id_hints.items():
        if key not in query_lower:
            continue
        for doc_id in doc_ids:
            if doc_id in ranked:
                continue
            try:
                doc = client.get_document(doc_id)
            except httpx.HTTPError:
                continue
            title = str(doc.get("title", "Untitled"))
            snippet = str(doc.get("text", ""))[:240]
            ranked[doc_id] = {
                "id": doc_id,
                "title": title,
                "snippet": snippet,
                "score": 12,
                "matched_term": "corpus-hint",
                "layer": _layer_hint(title, snippet),
            }

    ordered = sorted(ranked.values(), key=lambda item: int(item["score"]), reverse=True)
    return [
        {
            "id": str(item["id"]),
            "title": str(item["title"]),
            "snippet": str(item["snippet"]),
            "layer": str(item["layer"]),
            "matched_term": str(item["matched_term"]),
            "score": str(item["score"]),
        }
        for item in ordered[:max_results]
    ]


def search_corpus_docs(
    query: str,
    docs_dir,
    *,
    max_results: int = 5,
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    """Hybrid (BM25 + optional embeddings) search over local corpus docs.

    Expanded English terms from the nano model are folded in when available so
    Persian queries hit English docs.
    """
    if not docs_dir.exists():
        return []

    cfg = settings or get_settings()
    extra_terms = expand_query_terms(query, cfg)
    hits = hybrid_search(
        query,
        cfg,
        docs_dir=docs_dir,
        extra_terms=extra_terms,
        max_results=max_results,
    )
    return [
        {
            "id": str(hit["id"]),
            "title": str(hit["title"]),
            "path": str(hit["path"]),
            "layer": _layer_hint(str(hit["title"]), str(hit.get("excerpt", ""))),
            "score": str(hit["score"]),
        }
        for hit in hits
    ]


def create_outline_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
    max_docs: int | None = None,
    *,
    research_only: bool = False,
) -> list:
    client = OutlineClient(settings, cost)
    cfg = settings or get_settings()
    limit = max_docs or cfg.qa_agent_max_docs
    read_doc_ids: set[str] = set()

    @tool
    def outline_research_bundle(query: str, max_results: int = 8) -> str:
        """One-shot Outline research: tries multiple search terms, merges and ranks docs.

        Prefer this FIRST instead of many outline_search_titles calls.
        Returns doc IDs, titles, layer hints, and relevance scores.
        """
        if not cfg.outline_api_key:
            corpus_hits = search_corpus_docs(query, cfg.corpus_docs_dir, max_results=max_results, settings=cfg)
            if not corpus_hits:
                return "Outline API key not configured and no local corpus matches."
            lines = ["Local corpus matches (Outline unavailable):"]
            for hit in corpus_hits:
                lines.append(
                    f"- [{hit['title']}] id={hit['id']} layer={hit['layer']} score={hit['score']}"
                )
            return "\n".join(lines)

        docs = research_outline_documents(client, query, max_results=min(max_results, limit))
        if not docs:
            corpus_hits = search_corpus_docs(query, cfg.corpus_docs_dir, max_results=max_results, settings=cfg)
            if corpus_hits:
                lines = ["No Outline hits. Local corpus matches:"]
                for hit in corpus_hits:
                    lines.append(
                        f"- [{hit['title']}] id={hit['id']} layer={hit['layer']} score={hit['score']}"
                    )
                return "\n".join(lines)
            return (
                "No documents found. Try broader terms via this tool only — "
                "do not run many manual title searches."
            )

        lines = [
            f"Found {len(docs)} documents for query: {query}",
            "",
            "| ID | Title | Layer | Score | Matched term |",
            "|----|-------|-------|-------|--------------|",
        ]
        for doc in docs:
            lines.append(
                f"| {doc['id']} | {doc['title']} | {doc['layer']} | {doc['score']} | {doc['matched_term']} |"
            )
        lines.extend(
            [
                "",
                "Read up to 3 **distinct** doc IDs below (one per layer when possible):",
            ]
        )
        for doc in docs[: min(3, limit)]:
            lines.append(f"- READ NEXT: id={doc['id']} | {doc['title']} | layer={doc['layer']}")
        lines.extend(
            [
                "",
                "Call outline_get_document once per ID above. Do not re-read the same ID.",
                "Do not run extra search queries — IDs above are sufficient.",
            ]
        )
        return "\n".join(lines)

    @tool
    def outline_search_titles(query: str, max_results: int = 10) -> str:
        """Search Outline across titles and body (multi-term). Prefer outline_research_bundle first."""
        if not cfg.outline_api_key:
            corpus_hits = search_corpus_docs(query, cfg.corpus_docs_dir, max_results=max_results, settings=cfg)
            if not corpus_hits:
                return "No documents found."
            lines = ["Local corpus matches (Outline unavailable):"]
            for hit in corpus_hits:
                lines.append(
                    f"- [{hit['title']}] id={hit['id']} layer={hit['layer']} score={hit['score']}"
                )
            return "\n".join(lines)

        docs = research_outline_documents(client, query, max_results=min(max_results, limit))
        if not docs:
            return "No documents found."
        lines = []
        for doc in docs:
            lines.append(
                f"- [{doc['title']}] id={doc['id']} layer={doc['layer']} "
                f"score={doc['score']} matched={doc['matched_term']}"
            )
        return "\n".join(lines)

    @tool
    def outline_get_document(document_id: str) -> str:
        """Fetch Outline document content by ID. Call once per distinct ID from the bundle."""
        doc_id = document_id.strip()
        if doc_id in read_doc_ids:
            return (
                f"Document {doc_id} was already read in this run. "
                "Pick a different ID from outline_research_bundle — do not re-fetch."
            )
        read_doc_ids.add(doc_id)
        try:
            text = client.export_document_text(doc_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return (
                f"Could not fetch document '{doc_id}': Outline returned HTTP {status}. "
                "The ID is likely invalid or inaccessible. Do NOT retry this ID — "
                "pick a different ID from outline_research_bundle and continue writing "
                "scenarios from the documents you have."
            )
        except httpx.HTTPError as exc:
            return (
                f"Could not fetch document '{doc_id}': {type(exc).__name__}. "
                "Do NOT retry this ID — pick a different ID from outline_research_bundle "
                "and continue with the documents you have."
            )
        if cost:
            cost.add_source(f"doc:{doc_id}")
        max_chars = 6000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars for token budget]"
        return text

    @tool
    def corpus_search_docs(query: str, max_results: int = 5) -> str:
        """Search locally indexed corpus/docs markdown files when Outline search is insufficient."""
        hits = search_corpus_docs(query, cfg.corpus_docs_dir, max_results=max_results, settings=cfg)
        if not hits:
            return "No local corpus matches."
        lines = ["Local corpus matches:"]
        for hit in hits:
            lines.append(
                f"- [{hit['title']}] id={hit['id']} layer={hit['layer']} path={hit['path']}"
            )
        return "\n".join(lines)

    @tool
    def corpus_map() -> str:
        """One-page map of all corpus docs grouped by layer (titles + doc ids).

        Read this FIRST to orient cheaply, then fetch promising doc ids directly.
        """
        from qa_agent.corpus_map import load_corpus_map

        text = load_corpus_map(cfg)
        return text or "Corpus map unavailable (no local corpus docs indexed)."

    tools = [corpus_map, outline_research_bundle, outline_get_document, corpus_search_docs]
    if not research_only:
        tools.insert(1, outline_search_titles)
    return tools
