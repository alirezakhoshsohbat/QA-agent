from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from qa_agent.models.schemas import CostReport


# Rough pricing per 1M tokens (USD) — approximate for tracking
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    # Moonshot / Kimi
    "moonshot-v1-8k": (0.95, 4.0),
    "moonshot-v1-32k": (0.95, 4.0),
    "moonshot-v1-128k": (0.95, 4.0),
    "kimi-k2-0711-preview": (0.95, 4.0),
    "moonshotai/kimi-k2.7-code": (0.95, 4.0),
    "kimi-k2.7-code": (0.95, 4.0),
    # Z.ai / GLM
    "glm-5.2": (1.4, 4.4),
    "glm-4-plus": (0.5, 0.5),
    # Google Gemini
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini-2.0-flash": (0.1, 0.4),
}


def _pricing_key(model: str) -> str:
    """Extract lookup key from provider:model or openrouter slug.

    Tries the full name first, then strips a leading ``provider:`` prefix; the
    caller falls back to a default when the key is unknown.
    """
    if model in MODEL_PRICING:
        return model
    if ":" in model:
        _, name = model.split(":", 1)
        return name
    return model


class CostTracker:
    """Track API calls and token usage for a single agent run."""

    def __init__(self, budget: int = 1500) -> None:
        self.budget = budget
        self.report = CostReport()
        self._graphify_tokens_used = 0

    def record_outline_call(self) -> None:
        self.report.outline_api_calls += 1

    def record_github_call(self) -> None:
        self.report.github_api_calls += 1

    def record_gitlab_call(self) -> None:
        """Deprecated — use record_github_call."""
        self.record_github_call()

    def record_confluence_call(self) -> None:
        self.report.confluence_api_calls += 1

    def record_azure_call(self) -> None:
        self.report.azure_api_calls += 1

    def record_openapi_call(self) -> None:
        self.report.openapi_api_calls += 1

    def record_graphify_tokens(self, tokens: int) -> None:
        self._graphify_tokens_used += tokens
        self.report.graphify_query_tokens = self._graphify_tokens_used

    def record_llm_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.report.llm_input_tokens += input_tokens
        self.report.llm_output_tokens += output_tokens

    def record_triage(self, complexity: str, source: str, generate_tier: str) -> None:
        self.report.triage_complexity = complexity
        self.report.triage_source = source
        self.report.generate_tier = generate_tier

    def record_escalation(self) -> None:
        self.report.escalated = True
        self.report.generate_tier = "pro"

    def add_source(self, source: str) -> None:
        if source not in self.report.sources_used:
            self.report.sources_used.append(source)

    def graphify_budget_remaining(self) -> int:
        return max(0, self.budget - self._graphify_tokens_used)

    def estimate_usd(self, model: str = "gpt-4.1") -> float:
        model_key = _pricing_key(model)
        pricing = MODEL_PRICING.get(model_key, (2.0, 8.0))
        input_cost = (self.report.llm_input_tokens / 1_000_000) * pricing[0]
        output_cost = (self.report.llm_output_tokens / 1_000_000) * pricing[1]
        graphify_cost = (self.report.graphify_query_tokens / 1_000_000) * 0.5
        total = input_cost + output_cost + graphify_cost
        self.report.estimated_usd = round(total, 4)
        return self.report.estimated_usd

    def save(self, path: Path, model: str = "gpt-4.1") -> Path:
        self.estimate_usd(model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def append_to_global(self, graphify_out: Path) -> None:
        """Append run stats to graphify-out/cost.json style tracker."""
        cost_path = graphify_out / "cost.json"
        if cost_path.exists():
            data = json.loads(cost_path.read_text(encoding="utf-8"))
        else:
            data = {"runs": [], "total_input_tokens": 0, "total_output_tokens": 0}

        data["runs"].append(
            {
                "date": datetime.now(timezone.utc).isoformat(),
                "input_tokens": self.report.llm_input_tokens,
                "output_tokens": self.report.llm_output_tokens,
                "graphify_tokens": self.report.graphify_query_tokens,
                "estimated_usd": self.report.estimated_usd,
            }
        )
        data["total_input_tokens"] += self.report.llm_input_tokens
        data["total_output_tokens"] += self.report.llm_output_tokens
        graphify_out.mkdir(parents=True, exist_ok=True)
        cost_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def summarize_diff(diff_text: str, max_lines: int = 500) -> str:
    """Truncate or summarize large diffs to save tokens."""
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text

    head = lines[: max_lines // 2]
    tail = lines[-max_lines // 4 :]
    omitted = len(lines) - len(head) - len(tail)
    summary = "\n".join(head)
    summary += f"\n\n... [{omitted} lines omitted for token budget] ...\n\n"
    summary += "\n".join(tail)
    return summary
