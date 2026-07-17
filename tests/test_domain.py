from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa_agent import domain
from qa_agent.models.schemas import AcceptanceCriterion


@pytest.fixture(autouse=True)
def _reset_domain(monkeypatch):
    """Each test starts from a clean, default (built-in) domain profile."""
    monkeypatch.delenv(domain.DOMAIN_RULES_ENV, raising=False)
    domain.clear_cache()
    yield
    monkeypatch.delenv(domain.DOMAIN_RULES_ENV, raising=False)
    domain.clear_cache()


def _use_rules(monkeypatch, tmp_path: Path, data: dict) -> None:
    path = tmp_path / "domain_rules.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(domain.DOMAIN_RULES_ENV, str(path))
    domain.clear_cache()


def test_default_profile_is_search_pane():
    rules = domain.load_domain_rules()
    assert "search-pane" in rules.name
    assert rules.concrete_signal_patterns
    assert rules.forbidden_patterns
    assert "search pane" in rules.outline_search_aliases


def test_empty_profile_is_fully_generic(monkeypatch, tmp_path):
    _use_rules(monkeypatch, tmp_path, {})
    rules = domain.load_domain_rules()
    assert rules.name == "generic"
    assert rules.concrete_signal_patterns == []
    assert rules.forbidden_patterns == []
    assert rules.outline_search_aliases == {}
    assert rules.domain_context_pattern is None


def test_empty_profile_drops_domain_forbidden_checks(monkeypatch, tmp_path):
    from qa_agent.tools.output import validate_gherkin_structure

    content = (
        "Feature: X\n"
        "  @acceptance-AC-1\n"
        "  Scenario: s\n"
        "    Given BotMessageData.filters carries values\n"
        "    When x\n"
        "    Then y\n"
    )
    # Default profile flags the forbidden BotMessageData.filters pattern.
    assert any("Forbidden" in e for e in validate_gherkin_structure(content))
    # Generic profile has no such domain rule.
    _use_rules(monkeypatch, tmp_path, {})
    assert not any("Forbidden" in e for e in validate_gherkin_structure(content))


def test_custom_profile_recognizes_its_own_concrete_signals(monkeypatch, tmp_path):
    from qa_agent.tools.output import _scenario_has_concrete_signal

    step = "Then the cart quantity is 3"
    assert not _scenario_has_concrete_signal(step)  # not concrete under default
    _use_rules(
        monkeypatch,
        tmp_path,
        {"name": "cart", "concrete_signals": [r"\b(?:sku|quantity|cartId)\b"]},
    )
    assert _scenario_has_concrete_signal(step)


def test_custom_profile_drives_pr_query_focus(monkeypatch, tmp_path):
    from qa_agent.tools.github import _normalize_pr_query

    _use_rules(
        monkeypatch,
        tmp_path,
        {"pr_query_focus_patterns": [r"(?i)checkout\s+flow", r"(?i)cart"]},
    )
    assert _normalize_pr_query("please analyze the checkout flow PRs") == "checkout flow"


def test_custom_forbidden_pattern_uses_default_message(monkeypatch, tmp_path):
    from qa_agent.tools.output import validate_gherkin_structure

    _use_rules(
        monkeypatch,
        tmp_path,
        {"forbidden_patterns": [{"label": "raw secret", "pattern": r"(?i)password\s*="}]},
    )
    content = (
        "Feature: X\n"
        "  @acceptance-AC-1\n"
        "  Scenario: s\n"
        "    Given password=hunter2\n"
        "    When x\n"
        "    Then y\n"
    )
    errors = validate_gherkin_structure(content)
    assert any("Forbidden PRD pattern: raw secret" in e for e in errors)


def test_shipped_example_json_is_valid_and_loads(monkeypatch):
    example = Path(__file__).resolve().parent.parent / "domain_rules.example.json"
    assert example.exists(), "domain_rules.example.json should ship at repo root"
    monkeypatch.setenv(domain.DOMAIN_RULES_ENV, str(example))
    domain.clear_cache()
    rules = domain.load_domain_rules()
    assert rules.concrete_signal_patterns
    assert rules.forbidden_patterns
