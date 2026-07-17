"""Config-driven domain rules.

The QA agent is feature-agnostic: none of the domain-specific vocabulary
(endpoint names, error codes, doc IDs, PR path markers, forbidden wire shapes)
lives in code. It lives here as **data**, and can be fully replaced at runtime
by pointing ``QA_AGENT_DOMAIN_RULES`` at a JSON file (or dropping a
``domain_rules.json`` next to where the agent runs).

Resolution order for the active profile:

1. ``QA_AGENT_DOMAIN_RULES`` env var → explicit path to a JSON file.
2. ``./domain_rules.json`` in the current working directory.
3. The built-in :data:`DEFAULT_DOMAIN_RULES` profile below.

A JSON override only needs the keys it wants to change; every missing key falls
back to *empty* (not to the built-in profile), so a minimal ``{}`` file yields a
fully generic agent that relies solely on the generic heuristics in the tools.

Regex case-insensitivity is expressed inline with ``(?i)`` inside each pattern
string so JSON configs stay flag-free and portable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

DOMAIN_RULES_ENV = "QA_AGENT_DOMAIN_RULES"
DOMAIN_RULES_FILENAME = "domain_rules.json"


@dataclass
class FalsePositiveRule:
    """Drop file paths that match a generic token but not the actual feature.

    Applies only when *every* phrase in ``when_all`` is present in the active
    query phrases. A path is kept (never a false positive) if it contains any of
    ``keep_if_contains``; otherwise it is dropped when it contains any
    ``drop_markers`` substring.
    """

    when_all: tuple[str, ...]
    keep_if_contains: tuple[str, ...]
    drop_markers: tuple[str, ...]


@dataclass
class DomainRules:
    """Compiled, ready-to-use view of a domain profile."""

    name: str = "generic"
    # Free-text vocabulary/contract hint injected into the research + generate
    # prompts so domain knowledge stays data, not baked into prompt text.
    prompt_hint: str = ""

    # output.py — Gherkin quality gate + blocking contract checks
    concrete_signal_patterns: list[re.Pattern[str]] = field(default_factory=list)
    domain_context_pattern: re.Pattern[str] | None = None
    forbidden_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    extra_vague_phrases: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    # outline.py — doc research
    outline_search_aliases: dict[str, list[str]] = field(default_factory=dict)
    outline_doc_id_hints: dict[str, list[str]] = field(default_factory=dict)
    layer_hints: list[tuple[str, list[str]]] = field(default_factory=list)

    # github.py — PR research
    pr_search_aliases: dict[str, list[str]] = field(default_factory=dict)
    pr_path_markers: dict[str, list[str]] = field(default_factory=dict)
    pr_query_focus_patterns: list[re.Pattern[str]] = field(default_factory=list)
    pr_false_positives: list[FalsePositiveRule] = field(default_factory=list)


def _compile_many(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns]


def _alt_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """Compile ``\\b(?:a|b|c)\\b`` (case-insensitive) from a term list."""
    if not terms:
        return None
    alternation = "|".join(re.escape(t) if " " in t else t for t in terms)
    return re.compile(rf"(?i)\b(?:{alternation})\b")


def _forbidden(entries: list[dict[str, str]]) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for entry in entries:
        label = entry.get("label", "")
        message = entry.get("message") or f"Forbidden PRD pattern: {label}"
        out.append((message, re.compile(entry["pattern"])))
    return out


def _labelled(entries: list[dict[str, str]]) -> list[tuple[str, re.Pattern[str]]]:
    return [(e["label"], re.compile(e["pattern"])) for e in entries]


def _str_map(raw: dict[str, Any]) -> dict[str, list[str]]:
    return {str(k): [str(v) for v in vals] for k, vals in (raw or {}).items()}


def _build(data: dict[str, Any]) -> DomainRules:
    return DomainRules(
        name=str(data.get("name", "generic")),
        prompt_hint=str(data.get("prompt_hint", "")),
        concrete_signal_patterns=_compile_many(data.get("concrete_signals", [])),
        domain_context_pattern=_alt_pattern(data.get("domain_context_terms", [])),
        forbidden_patterns=_forbidden(data.get("forbidden_patterns", [])),
        extra_vague_phrases=_labelled(data.get("vague_phrases", [])),
        outline_search_aliases=_str_map(data.get("outline_search_aliases", {})),
        outline_doc_id_hints=_str_map(data.get("outline_doc_id_hints", {})),
        layer_hints=[
            (str(h["layer"]), [str(t).lower() for t in h.get("terms", [])])
            for h in data.get("layer_hints", [])
        ],
        pr_search_aliases=_str_map(data.get("pr_search_aliases", {})),
        pr_path_markers=_str_map(data.get("pr_path_markers", {})),
        pr_query_focus_patterns=[re.compile(p) for p in data.get("pr_query_focus_patterns", [])],
        pr_false_positives=[
            FalsePositiveRule(
                when_all=tuple(str(t).lower() for t in fp.get("when_all", [])),
                keep_if_contains=tuple(str(t).lower() for t in fp.get("keep_if_contains", [])),
                drop_markers=tuple(str(t).lower() for t in fp.get("drop_markers", [])),
            )
            for fp in data.get("pr_false_positives", [])
        ],
    )


def _settings_path() -> str:
    """Read the domain-rules path from Settings (covers .env when the process
    did not call load_dotenv, e.g. the web server). Best-effort."""
    try:
        from qa_agent.config import get_settings

        return (get_settings().qa_agent_domain_rules or "").strip()
    except Exception:
        return ""


def _resolve_path() -> Path | None:
    configured = os.getenv(DOMAIN_RULES_ENV, "").strip() or _settings_path()
    if configured:
        return Path(configured).expanduser()
    local = Path.cwd() / DOMAIN_RULES_FILENAME
    if local.exists():
        return local
    return None


@lru_cache(maxsize=None)
def load_domain_rules() -> DomainRules:
    """Return the active domain profile (cached).

    Call :func:`clear_cache` after changing ``QA_AGENT_DOMAIN_RULES`` in-process.
    """
    path = _resolve_path()
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid domain rules file at {path}: {exc}") from exc
        return _build(data)
    return _build(DEFAULT_DOMAIN_RULES)


def clear_cache() -> None:
    load_domain_rules.cache_clear()


# ---------------------------------------------------------------------------
# Built-in default profile.
#
# This ships as an *example* tuned for the "search pane" agent domain. It is
# data, not behavior: replace it wholesale with a JSON file for any other
# product. Case-insensitive matches use an inline ``(?i)`` prefix.
# ---------------------------------------------------------------------------
DEFAULT_DOMAIN_RULES: dict[str, Any] = {
    "name": "search-pane (example)",
    "prompt_hint": (
        "Common wire vocabulary for this domain: endpoints such as /api/bot/init "
        "and /api/bot/send-message; fields data.filters, search_filters, "
        "SearchCriteria, propertyType, priceRange, rooms, baths, municipalities; "
        "error codes VALIDATION_ERROR, UNAUTHORIZED, BOT_ACCOUNT_NOT_FOUND. "
        "Contract rules: priceRange must be an object with min/max (never a string "
        "bucket); a history/BotMessageData message must never carry filter values."
    ),
    # Domain identifiers/vocabulary that make a Gherkin step "concrete". These
    # are unioned with the generic structural signals baked into output.py.
    "concrete_signals": [
        r"\b(?:data|event)\.filters\b",
        r"\bsearch_filters\b",
        r"\b(?:sanitizeSearchCriteriaFields|sortSearchPaneFields|updateCriteria|useSearchCriteria)\b",
        r"\bSearchPaneFilterId\b",
        r"\bSearchCriteria\b",
        r"\bBotMessageData\b",
        r"\b(?:send_text|propertyType|priceRange|rooms|baths|municipalities)\b",
        r"\bVALIDATION_ERROR\b",
        r"\b(?:UNAUTHORIZED|BOT_ACCOUNT_NOT_FOUND|BOT_SESSION_NOT_READY|BOT_UPSTREAM_ERROR)\b",
        r"\bSSE\b",
        r"(?i)\bprofile update\b",
        r"(?i)\bfilters with (?:unknown|invalid)\b",
        r"(?i)\b(?:unknown|invalid)\s+IDs?\b",
        r"(?i)\bchat message\b",
        r"\bsend-message\b",
        r"(?i)\bupstream\b",
    ],
    # Terms that keep an otherwise-"abstract" step from being flagged (context
    # that makes "handles"/"updates" acceptable).
    "domain_context_terms": [
        "filters",
        "search_filters",
        "SearchPane",
        "SearchCriteria",
        "BFF",
        "profile update",
        "send-message",
    ],
    # Blocking contract violations. A missing "message" defaults to
    # "Forbidden PRD pattern: {label}".
    "forbidden_patterns": [
        {
            "label": "BotMessageData.filters property",
            "pattern": r"(?i)BotMessageData\.filters\b",
        },
        {
            "label": "History message must not carry filter values",
            "pattern": r"(?i)(?:last|assistant)\s+message\s+containing\s+filters\s*\{",
        },
        {
            "label": "message containing filters with values",
            "pattern": r"(?i)message\s+containing\s+filters\s*\{[^}]*[\"'][\w.]+[\"']\s*:",
        },
        {
            "label": "priceRange string bucket",
            "message": "Forbidden wire shape: priceRange must be an object with min/max, not a string bucket",
            "pattern": r"(?i)[\"']priceRange[\"']\s*:\s*[\"'][\d-]+[\"']",
        },
    ],
    # Extra vague phrases beyond the generic ones in output.py.
    "vague_phrases": [
        {
            "label": "processes filters (alone)",
            "pattern": r"(?i)\bprocesses the filters\b(?!\s*(array|list|from))",
        },
    ],
    # outline.py — curated Outline search terms per feature phrase.
    "outline_search_aliases": {
        "search pane": ["Search Pane", "pane", "search-pane", "search-pane-sync", "send-message"],
        "search-pane": ["Search Pane", "pane", "search-pane", "search-pane-sync"],
    },
    # Instance-specific Outline document IDs used only as a last-resort fallback
    # when search misses. These belong to one Outline workspace — override for
    # yours (or leave empty).
    "outline_doc_id_hints": {
        "search pane": [
            "f57d80cf-bdcf-4631-8a9d-c7440d7555a6",
            "f8e00124-aeec-4903-85e0-c7977ea98669",
            "0697e61e-b000-4252-ab05-7717e3cf1a1f",
        ],
    },
    # Ordered layer classification. First layer whose any term appears in the
    # doc title+snippet wins. Generic terms let unknown domains still classify.
    "layer_hints": [
        {"layer": "bff", "terms": ["bff", "search-pane-sync", "/api/bot"]},
        {"layer": "frontend", "terms": ["frontend", "agent sync", "localstorage"]},
        {"layer": "bff", "terms": ["send-message", "profile"]},
        {"layer": "e2e", "terms": ["e2e", "end-to-end", "cypress", "playwright"]},
        {"layer": "backend", "terms": ["backend", "server", "api contract"]},
    ],
    # github.py — curated PR search terms + path markers per feature phrase.
    "pr_search_aliases": {
        "search pane": [
            "search-pane",
            "search-pane-sync",
            "search-pane-filters",
            "SearchPane",
            "agent sync",
            "searchpane",
        ],
        "search-pane": ["search pane", "search-pane-sync", "search-pane-filters", "SearchPane"],
    },
    "pr_path_markers": {
        "search pane": [
            "search-pane",
            "searchpane",
            "search_pane",
            "search-pane-sync",
            "search-pane-filters",
            "searchpanefilter",
            "docs/prd/agent/frontend/search-pane",
            "docs/prd/agent/bff/search-pane",
            "api/bot/search-pane",
            "lib/bot/search-pane",
            "pages/api/bot/send-message",
        ],
        "search-pane": [
            "search-pane",
            "search-pane-sync",
            "search-pane-filters",
            "docs/prd/agent/frontend/search-pane",
            "docs/prd/agent/bff/search-pane",
        ],
    },
    # Focus a long delegation blob down to a short feature phrase.
    "pr_query_focus_patterns": [
        r"(?i)search\s+pane",
        r"(?i)search-pane",
        r"(?i)login\s+(?:flow|auth)",
        r"(?i)send-message",
        r"(?i)payment\s+flow",
    ],
    "pr_false_positives": [
        {
            "when_all": ["search", "pane"],
            "keep_if_contains": ["search-pane", "searchpane", "search_pane"],
            "drop_markers": [
                "/api/v3/search/",
                "pages/api/v3/search",
                "/search/index.ts",
                "/search/index.tsx",
            ],
        },
    ],
}
