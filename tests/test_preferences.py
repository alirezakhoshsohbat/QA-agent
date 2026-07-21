"""Tests for UI preference store (overrides .env operational knobs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa_agent.config import Settings, get_settings
from qa_agent.preferences import (
    UI_MANAGED_FIELDS,
    apply_preferences,
    is_secret_unchanged,
    load_preferences,
    mask_secret,
    preferences_snapshot,
    sanitize_preferences,
    save_preferences,
)


def test_sanitize_clamps_ints_and_coerces_bools():
    cleaned = sanitize_preferences(
        {
            "qa_agent_max_write_attempts": 99,
            "qa_agent_query_expansion": "yes",
            "qa_agent_triage": 0,
            "qa_agent_outline_subagent": "false",
            "qa_agent_research_model": "  gpt-4.1-mini  ",
            "unknown_field": "ignore-me",
        }
    )
    assert cleaned["qa_agent_max_write_attempts"] == 12
    assert cleaned["qa_agent_query_expansion"] is True
    assert cleaned["qa_agent_triage"] is False
    assert cleaned["qa_agent_outline_subagent"] is False
    assert cleaned["qa_agent_research_model"] == "gpt-4.1-mini"
    assert "unknown_field" not in cleaned


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    save_preferences(
        {
            "qa_agent_pro_model": "gpt-5",
            "qa_agent_max_reprompts": 10,
            "qa_agent_escalation": False,
            "qa_agent_github_subagent": False,
            "llm_base_url": "https://api.avalai.ir/v1",
            "llm_api_key": "aa-secret-key-123456",
        }
    )
    prefs = load_preferences()
    assert prefs["qa_agent_pro_model"] == "gpt-5"
    assert prefs["qa_agent_max_reprompts"] == 10
    assert prefs["qa_agent_escalation"] is False
    assert prefs["qa_agent_github_subagent"] is False
    assert prefs["llm_base_url"] == "https://api.avalai.ir/v1"
    assert prefs["llm_api_key"] == "aa-secret-key-123456"


def test_blank_secret_keeps_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    save_preferences({"llm_api_key": "aa-original-key-zzzz"})
    current = Settings(llm_api_key="aa-original-key-zzzz")
    save_preferences(
        {"llm_api_key": "", "llm_base_url": "https://new.example/v1"},
        current_settings=current,
    )
    prefs = load_preferences()
    assert prefs["llm_api_key"] == "aa-original-key-zzzz"
    assert prefs["llm_base_url"] == "https://new.example/v1"


def test_masked_secret_is_unchanged():
    current = "aa-Smjg6UqYbvUILkBfI4szfVnzjNjGqOYvphcq1isffsqup8vi"
    masked = mask_secret(current)
    assert "•" in masked
    assert is_secret_unchanged(masked, current)
    assert is_secret_unchanged("", current)
    assert not is_secret_unchanged("brand-new-key", current)


def test_apply_preferences_overlays_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    save_preferences(
        {
            "qa_agent_generate_model": "gpt-5",
            "qa_agent_token_budget": 3000,
            "llm_api_key": "from-ui",
            "qa_agent_outline_subagent": False,
        }
    )
    base = Settings(
        qa_agent_generate_model="gpt-4.1",
        qa_agent_token_budget=1500,
        llm_api_key="from-env",
        llm_base_url="http://example",
        qa_agent_model_profile="router",
        qa_agent_outline_subagent=True,
    )
    merged = apply_preferences(base)
    assert merged.qa_agent_generate_model == "gpt-5"
    assert merged.qa_agent_token_budget == 3000
    assert merged.llm_api_key == "from-ui"
    assert merged.qa_agent_outline_subagent is False
    assert merged.qa_agent_max_docs == base.qa_agent_max_docs


def test_preferences_snapshot_masks_secrets():
    settings = Settings(llm_api_key="aa-very-secret-key-9999", outline_api_key="")
    snap = preferences_snapshot(settings)
    assert set(UI_MANAGED_FIELDS).issubset(set(snap.keys()))
    assert snap["llm_api_key_set"] is True
    assert "•" in snap["llm_api_key"]
    assert "very-secret" not in snap["llm_api_key"]
    assert snap["outline_api_key_set"] is False
    assert snap["outline_api_key"] == ""


def test_get_settings_reads_ui_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "QA_AGENT_MODEL_PROFILE=router\n"
        "LLM_API_KEY=test\n"
        "LLM_BASE_URL=http://localhost\n"
        "QA_AGENT_GENERATE_MODEL=from-env\n",
        encoding="utf-8",
    )
    save_preferences(
        {
            "qa_agent_generate_model": "from-ui",
            "qa_agent_github_subagent": False,
            "llm_base_url": "https://api.avalai.ir/v1",
        }
    )
    settings = get_settings()
    assert settings.qa_agent_generate_model == "from-ui"
    assert settings.qa_agent_github_subagent is False
    assert settings.llm_base_url == "https://api.avalai.ir/v1"
    assert settings.qa_agent_project_id == "default"
    assert "projects" in str(settings.qa_agent_output_dir).replace("\\", "/")
    assert (tmp_path / "projects" / "default" / "preferences.json").exists()
