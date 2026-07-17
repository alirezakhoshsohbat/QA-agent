"""LLM query understanding: turn an arbitrary (often Persian) QA request into
English search terms for Outline/corpus retrieval.

Uses the cheap "nano" model tier with a small JSON contract. Results are cached
on disk per query, and every failure path falls back to an empty list so the
caller can degrade to heuristic term derivation — a broken/missing LLM must
never break search.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from qa_agent.config import Settings, get_settings

_CACHE_NAMESPACE = "query_expansion"
_CACHE_TTL_SECONDS = 7 * 86400
_MAX_TERMS = 8

_EXPANSION_PROMPT = """You convert a QA/test request into search terms for an \
English technical wiki (product docs, PRDs, API specs).

Request (may be Persian or English):
{query}

Return ONLY a JSON object, no prose, no code fences:
{{"terms": ["..."], "entities": ["..."]}}

Rules:
- "terms": 3-6 short ENGLISH search phrases (1-3 words each) covering the
  feature name, synonyms, and the underlying domain concepts. Translate Persian
  concepts to the English terms a developer wiki would use.
- "entities": exact feature/component names mentioned verbatim (keep original
  casing/kebab-case, e.g. "search-pane", "home page v2").
- No generic words like: test, case, scenario, feature, flow, write.
"""


def _cache_path(settings: Settings, query: str) -> Path:
    digest = hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:16]
    return settings.cache_dir / _CACHE_NAMESPACE / f"{digest}.json"


def _read_cache(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    try:
        import time

        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload.get("_cached_at", 0) > _CACHE_TTL_SECONDS:
            return None
        terms = payload.get("terms")
        return terms if isinstance(terms, list) else None
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, terms: list[str]) -> None:
    try:
        import time

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"_cached_at": time.time(), "terms": terms}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def parse_expansion_response(raw: str) -> list[str]:
    """Extract search terms from the model's JSON reply (tolerates code fences)."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []

    terms: list[str] = []
    for bucket in ("entities", "terms"):
        values = payload.get(bucket)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                terms.append(value.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped[:_MAX_TERMS]


def expand_query_terms(query: str, settings: Settings | None = None) -> list[str]:
    """English search terms for a QA request via the nano model; [] on failure."""
    settings = settings or get_settings()
    if not settings.qa_agent_query_expansion or not query.strip():
        return []

    cache_file = _cache_path(settings, query)
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached[:_MAX_TERMS]

    try:
        from qa_agent.models.llm import create_nano_model

        model = create_nano_model(settings)
        response = model.invoke(_EXPANSION_PROMPT.format(query=query.strip()[:800]))
        raw = getattr(response, "content", "")
        if isinstance(raw, list):  # some providers return content blocks
            raw = " ".join(str(part) for part in raw)
        terms = parse_expansion_response(str(raw))
    except Exception:
        # Any provider/credential/parse error → heuristic fallback at call site.
        return []

    if terms:
        _write_cache(cache_file, terms)
    return terms
