from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class TestCaseRequest(BaseModel):
    query: str
    token_budget: int = 1500
    output_dir: Path = Path("./output")


class SourceReference(BaseModel):
    kind: str
    id: str
    title: str = ""
    url: str = ""


class AcceptanceCriterion(BaseModel):
    id: str
    layer: str = ""
    summary: str = ""


class CoverageReport(BaseModel):
    checklist_total: int = 0
    covered: int = 0
    missing: list[str] = Field(default_factory=list)
    wip: list[str] = Field(default_factory=list)


class GeneratedFeature(BaseModel):
    slug: str
    feature_content: str
    sources: list[SourceReference] = Field(default_factory=list)
    query: str = ""
    acceptance_checklist: list[AcceptanceCriterion] = Field(default_factory=list)


class CostReport(BaseModel):
    graphify_query_tokens: int = 0
    outline_api_calls: int = 0
    gitlab_api_calls: int = 0  # deprecated alias
    github_api_calls: int = 0
    confluence_api_calls: int = 0
    azure_api_calls: int = 0
    openapi_api_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    estimated_usd: float = 0.0
    sources_used: list[str] = Field(default_factory=list)
    # Tier routing audit trail
    triage_complexity: str = ""
    triage_source: str = ""
    generate_tier: str = ""
    escalated: bool = False


def slugify(text: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_length] or "feature"


class FileCache:
    """Simple TTL file cache for API responses."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        safe_key = re.sub(r"[^\w.-]", "_", key)
        return self.base_dir / namespace / f"{safe_key}.json"

    def get(self, namespace: str, key: str, ttl_seconds: int) -> Any | None:
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - payload.get("_cached_at", 0) > ttl_seconds:
                return None
            return payload.get("data")
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, namespace: str, key: str, data: Any) -> None:
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"_cached_at": time.time(), "data": data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
