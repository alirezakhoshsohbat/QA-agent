"""Multi-project isolation and registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa_agent.config import get_settings
from qa_agent.preferences import load_preferences, save_preferences
from qa_agent.projects import (
    activate_project,
    create_project,
    delete_project,
    ensure_projects,
    get_active_project_id,
    list_projects,
    preferences_path_for,
)


def test_ensure_projects_creates_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    ensure_projects()
    projects = list_projects()
    assert len(projects) == 1
    assert projects[0].id == "default"
    assert get_active_project_id() == "default"
    assert preferences_path_for("default").exists()
    assert (tmp_path / "projects" / "default" / "output").is_dir()


def test_migrate_legacy_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "ui_preferences.json").write_text(
        '{"qa_agent_token_budget": 2500}\n',
        encoding="utf-8",
    )
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "legacy.feature").write_text("Feature: legacy\n", encoding="utf-8")
    ensure_projects()
    prefs = load_preferences("default")
    assert prefs["qa_agent_token_budget"] == 2500
    assert (tmp_path / "projects" / "default" / "output" / "legacy.feature").exists()
    assert not (tmp_path / ".cache" / "ui_preferences.json").exists()


def test_project_isolation_prefs_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    ensure_projects()
    a = create_project("Alpha")
    b = create_project("Beta")
    activate_project(a.id)
    save_preferences({"qa_agent_token_budget": 2222, "qa_agent_outline_subagent": False})
    activate_project(b.id)
    save_preferences({"qa_agent_token_budget": 3333, "qa_agent_outline_subagent": True})

    settings_a = get_settings(a.id)
    settings_b = get_settings(b.id)
    assert settings_a.qa_agent_token_budget == 2222
    assert settings_a.qa_agent_outline_subagent is False
    assert settings_b.qa_agent_token_budget == 3333
    assert settings_b.qa_agent_outline_subagent is True
    assert settings_a.qa_agent_output_dir != settings_b.qa_agent_output_dir
    assert "projects" in str(settings_a.qa_agent_output_dir).replace("\\", "/")
    assert a.id in str(settings_a.qa_agent_output_dir)
    assert b.id in str(settings_b.cache_dir)


def test_cannot_delete_active_or_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    ensure_projects()
    other = create_project("Other")
    with pytest.raises(ValueError, match="active"):
        delete_project(get_active_project_id())
    activate_project(other.id)
    delete_project("default")
    with pytest.raises(ValueError, match="last"):
        delete_project(other.id)
