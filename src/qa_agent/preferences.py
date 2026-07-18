"""UI-managed preferences that override .env defaults.

Operational knobs, credentials, and sub-agent toggles are editable from the
Web Settings panel and persisted under ``.cache/ui_preferences.json``. Values
here take precedence over ``.env`` when present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Fields the Settings modal can read/write. Keep in sync with the UI form.
UI_MANAGED_FIELDS: tuple[str, ...] = (
    # Connection / credentials
    "llm_api_key",
    "llm_base_url",
    "qa_agent_research_base_url",
    "qa_agent_generate_base_url",
    "qa_agent_embedding_base_url",
    "qa_agent_embedding_api_key",
    "outline_api_key",
    "outline_base_url",
    "github_token",
    "github_base_url",
    "github_repos",
    "confluence_base_url",
    "confluence_email",
    "confluence_api_token",
    "confluence_space",
    "confluence_auth_mode",
    "azure_devops_pat",
    "azure_devops_org",
    "azure_devops_project",
    "azure_devops_base_url",
    "azure_devops_repos",
    "azure_devops_wiki",
    "azure_devops_api_version",
    "openapi_specs",
    "openapi_token",
    # TLS
    "qa_agent_verify_tls",
    # Model ladder
    "qa_agent_research_model",
    "qa_agent_generate_model",
    "qa_agent_nano_model",
    "qa_agent_pro_model",
    "qa_agent_embedding_model",
    # Smart routing
    "qa_agent_query_expansion",
    "qa_agent_triage",
    "qa_agent_escalation",
    # Sub-agents
    "qa_agent_outline_subagent",
    "qa_agent_github_subagent",
    "qa_agent_confluence_subagent",
    "qa_agent_azure_subagent",
    "qa_agent_openapi_subagent",
    # Persistence
    "qa_agent_max_write_attempts",
    "qa_agent_max_reprompts",
    "qa_agent_quality_refine_rounds",
    # Research / cost budgets
    "qa_agent_token_budget",
    "qa_agent_max_prs",
    "qa_agent_pr_scan_limit",
    "qa_agent_max_prs_deep",
    "qa_agent_max_docs",
    "qa_agent_max_diff_lines",
    "qa_agent_recursion_limit",
)

SECRET_FIELDS = frozenset(
    {
        "llm_api_key",
        "qa_agent_embedding_api_key",
        "outline_api_key",
        "github_token",
        "confluence_api_token",
        "azure_devops_pat",
        "openapi_token",
    }
)

_BOOL_FIELDS = frozenset(
    {
        "qa_agent_query_expansion",
        "qa_agent_triage",
        "qa_agent_escalation",
        "qa_agent_outline_subagent",
        "qa_agent_github_subagent",
        "qa_agent_confluence_subagent",
        "qa_agent_azure_subagent",
        "qa_agent_openapi_subagent",
        "qa_agent_verify_tls",
    }
)

# Blank in the UI means "keep the Settings default / .env value", not an
# explicit empty override. Storing "" would wipe required model names.
_EMPTY_CLEARS_OVERRIDE = frozenset(
    {
        "qa_agent_research_model",
        "qa_agent_generate_model",
        "qa_agent_nano_model",
        "qa_agent_pro_model",
        "qa_agent_embedding_model",
        "qa_agent_research_base_url",
        "qa_agent_generate_base_url",
        "qa_agent_embedding_base_url",
    }
)

_INT_FIELDS = frozenset(
    {
        "qa_agent_max_write_attempts",
        "qa_agent_max_reprompts",
        "qa_agent_quality_refine_rounds",
        "qa_agent_token_budget",
        "qa_agent_max_prs",
        "qa_agent_pr_scan_limit",
        "qa_agent_max_prs_deep",
        "qa_agent_max_docs",
        "qa_agent_max_diff_lines",
        "qa_agent_recursion_limit",
    }
)

_FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    "qa_agent_max_write_attempts": (1, 12),
    "qa_agent_max_reprompts": (1, 20),
    "qa_agent_quality_refine_rounds": (0, 6),
    "qa_agent_token_budget": (200, 8000),
    "qa_agent_max_prs": (1, 30),
    "qa_agent_pr_scan_limit": (5, 200),
    "qa_agent_max_prs_deep": (1, 15),
    "qa_agent_max_docs": (1, 20),
    "qa_agent_max_diff_lines": (50, 5000),
    "qa_agent_recursion_limit": (10, 120),
}


def preferences_path() -> Path:
    return Path(".cache") / "ui_preferences.json"


def mask_secret(value: str) -> str:
    """Display form for secrets — never return the full key to the browser."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "••••••••"
    return f"{text[:3]}••••••••{text[-4:]}"


def is_secret_unchanged(raw: Any, current: str) -> bool:
    """True when the client left the secret blank or sent back the masked form."""
    if raw is None:
        return True
    text = str(raw).strip()
    if not text:
        return True
    if "•" in text:
        return True
    return text == mask_secret(current)


def load_preferences() -> dict[str, Any]:
    path = preferences_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in UI_MANAGED_FIELDS}


def save_preferences(
    updates: dict[str, Any],
    *,
    current_settings: Any | None = None,
) -> dict[str, Any]:
    """Merge ``updates`` into the on-disk preference file and return the result.

    Secret fields left blank (or sent as the masked display value) keep their
    previously saved / env value and are not overwritten with empty strings.
    """
    current = load_preferences()
    cleaned = sanitize_preferences(updates)

    for key in SECRET_FIELDS:
        if key not in cleaned:
            continue
        existing = ""
        if current_settings is not None:
            existing = str(getattr(current_settings, key, "") or "")
        elif key in current:
            existing = str(current.get(key) or "")
        if is_secret_unchanged(cleaned[key], existing):
            cleaned.pop(key, None)

    for key in list(cleaned.keys()):
        if cleaned[key] is None:
            cleaned.pop(key, None)
            current.pop(key, None)

    current.update(cleaned)
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return current


def sanitize_preferences(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce and clamp a partial preference payload; drop unknown keys."""
    cleaned: dict[str, Any] = {}
    for key in UI_MANAGED_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _BOOL_FIELDS:
            cleaned[key] = _as_bool(value)
        elif key in _INT_FIELDS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            lo, hi = _FIELD_BOUNDS[key]
            cleaned[key] = max(lo, min(hi, number))
        else:
            text = str(value).strip() if value is not None else ""
            if key in _EMPTY_CLEARS_OVERRIDE and not text:
                # Sentinel: caller should delete this key from stored prefs.
                cleaned[key] = None
            else:
                cleaned[key] = text
    return cleaned


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_preferences(settings: Any) -> Any:
    """Return a copy of ``settings`` with UI preferences overlaid.

    Empty strings for model/URL override fields are ignored so they cannot
    wipe Settings defaults (e.g. blank Research model → keep .env default).
    """
    prefs = load_preferences()
    if not prefs:
        return settings
    update = {
        k: v
        for k, v in prefs.items()
        if not (k in _EMPTY_CLEARS_OVERRIDE and (v is None or str(v).strip() == ""))
    }
    if not update:
        return settings
    return settings.model_copy(update=update)


def preferences_snapshot(settings: Any) -> dict[str, Any]:
    """Effective values for every UI-managed field (env defaults + prefs).

    Secrets are masked for safe delivery to the browser.
    """
    snap: dict[str, Any] = {}
    for key in UI_MANAGED_FIELDS:
        value = getattr(settings, key)
        if key in SECRET_FIELDS:
            text = str(value or "")
            snap[key] = mask_secret(text)
            snap[f"{key}_set"] = bool(text.strip())
        elif hasattr(value, "__fspath__"):
            snap[key] = str(value)
        else:
            snap[key] = value
    return snap
