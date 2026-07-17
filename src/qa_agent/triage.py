"""Complexity triage: decide which generate tier a QA request needs.

A nano-model classification (with a deterministic heuristic fallback) labels
the request simple/standard/complex. Complex requests use the "pro" model tier
from the start; everything else stays on the default generate model. The
decision is recorded in the run's cost report so tier routing stays auditable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from qa_agent.config import Settings, get_settings

_TRIAGE_PROMPT = """Classify the complexity of this QA test-generation request.

Request:
{query}

Return ONLY a JSON object, no prose, no code fences:
{{"complexity": "simple|standard|complex", "reason": "..."}}

Guidance:
- simple: one narrow feature or single happy-path flow.
- standard: one feature with edge cases, or a typical full-coverage ask.
- complex: multiple features/layers at once, full max-coverage across a large
  surface, cross-system integration, migration/compliance scope, or a PR with
  a very large diff.
Keep "reason" under 20 words.
"""

# Signals that a request spans a large surface (English + Persian).
_BROAD_SCOPE_MARKERS = (
    "all ", "every", "entire", "full coverage", "max coverage", "end-to-end",
    "e2e", "integration", "migration",
    "تمام", "همه", "کامل", "حداکثر", "سرتاسر", "یکپارچه",
)


@dataclass(frozen=True)
class TriageDecision:
    complexity: str  # simple | standard | complex
    reason: str
    generate_role: str  # generate | pro
    source: str  # llm | heuristic | disabled


def _role_for(complexity: str) -> str:
    return "pro" if complexity == "complex" else "generate"


def heuristic_triage(query: str) -> TriageDecision:
    """Deterministic fallback: scope markers + enumeration density."""
    normalized = query.lower()
    score = 0
    for marker in _BROAD_SCOPE_MARKERS:
        if marker in normalized:
            score += 2
    # Many conjunctions/separators usually mean multiple features in one ask.
    separators = len(re.findall(r"[,،;]|\bو\b|\band\b", normalized))
    if separators >= 4:
        score += 2
    elif separators >= 2:
        score += 1
    if len(query) > 400:
        score += 1

    if score >= 4:
        complexity = "complex"
    elif score >= 2:
        complexity = "standard"
    else:
        complexity = "simple"
    return TriageDecision(
        complexity=complexity,
        reason=f"heuristic score={score}",
        generate_role=_role_for(complexity),
        source="heuristic",
    )


def parse_triage_response(raw: str) -> tuple[str, str] | None:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    complexity = str(payload.get("complexity", "")).strip().lower()
    if complexity not in ("simple", "standard", "complex"):
        return None
    return complexity, str(payload.get("reason", ""))[:200]


def triage_request(query: str, settings: Settings | None = None) -> TriageDecision:
    """Classify request complexity; never raises."""
    settings = settings or get_settings()
    if not settings.qa_agent_triage:
        return TriageDecision(
            complexity="standard",
            reason="triage disabled",
            generate_role="generate",
            source="disabled",
        )

    try:
        from qa_agent.models.llm import create_nano_model

        model = create_nano_model(settings)
        response = model.invoke(_TRIAGE_PROMPT.format(query=query.strip()[:1200]))
        raw = getattr(response, "content", "")
        if isinstance(raw, list):
            raw = " ".join(str(part) for part in raw)
        parsed = parse_triage_response(str(raw))
        if parsed:
            complexity, reason = parsed
            return TriageDecision(
                complexity=complexity,
                reason=reason,
                generate_role=_role_for(complexity),
                source="llm",
            )
    except Exception:
        pass

    return heuristic_triage(query)
