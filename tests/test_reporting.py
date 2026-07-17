from __future__ import annotations

from pathlib import Path

from qa_agent.reporting import (
    build_traceability,
    coverage_gap,
    parse_scenarios,
    scan_existing_tests,
    traceability_csv,
    traceability_html,
)

MULTI_LAYER = """@layer-frontend
Feature: Search Pane Frontend Sync
  @acceptance-E-1 @doc-f57d80cf
  Scenario: Init renders listed chips
    Given GET /api/bot/init returns data.filters = ["rooms"]
    When the Search Pane renders
    Then the rooms chip is visible

@layer-bff
Feature: Search Pane BFF Send
  @acceptance-B-1 @doc-f8e00124 @pr-42
  Scenario: Send forwards content only
    Given POST /api/bot/send-message with body { "message": "hi" }
    When the BFF processes it
    Then the upstream body equals { "content": "hi" }

  @wip-B-2
  Scenario: Ambiguous profile failure
    Given the profile update behavior is undocumented
    When POST /api/bot/send-message is issued
    Then behavior is pending clarification
"""


def test_parse_scenarios_associates_tags_and_layers():
    scenarios = parse_scenarios(MULTI_LAYER)
    assert len(scenarios) == 3
    first = scenarios[0]
    assert first["acceptance"] == ["E-1"]
    assert first["docs"] == ["f57d80cf"]
    assert first["layers"] == ["frontend"]

    second = scenarios[1]
    assert second["acceptance"] == ["B-1"]
    assert second["prs"] == ["42"]
    assert second["layers"] == ["bff"]

    third = scenarios[2]
    assert third["wip"] == ["B-2"]


def test_build_traceability_statuses():
    checklist = [
        {"id": "E-1", "layer": "frontend", "summary": "init chips"},
        {"id": "B-1", "layer": "bff", "summary": "send content"},
        {"id": "B-2", "layer": "bff", "summary": "profile failure"},
        {"id": "B-9", "layer": "bff", "summary": "never covered"},
    ]
    rows = build_traceability(MULTI_LAYER, checklist)
    by_id = {r["id"]: r for r in rows}
    assert by_id["E-1"]["status"] == "covered"
    assert by_id["B-1"]["status"] == "covered"
    assert by_id["B-2"]["status"] == "wip"
    assert by_id["B-9"]["status"] == "missing"


def test_traceability_csv_and_html():
    checklist = [{"id": "E-1", "layer": "frontend", "summary": "init chips"}]
    rows = build_traceability(MULTI_LAYER, checklist)
    csv_text = traceability_csv(rows)
    assert "Acceptance ID" in csv_text
    assert "E-1" in csv_text
    html_text = traceability_html(rows)
    assert "<table>" in html_text
    assert "E-1" in html_text


def test_scan_existing_tests_and_coverage_gap(tmp_path):
    (tmp_path / "existing.feature").write_text(
        "Feature: Auth\n  Scenario: Init renders listed chips\n    Given x\n    When y\n    Then z\n",
        encoding="utf-8",
    )
    (tmp_path / "login.test.js").write_text(
        "describe('auth', () => { it('send forwards content only', () => {}); });",
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignore.test.js").write_text(
        "it('should be ignored', () => {});", encoding="utf-8"
    )

    existing = scan_existing_tests(tmp_path)
    titles = {item["title"] for item in existing}
    assert "Init renders listed chips" in titles
    assert "send forwards content only" in titles
    assert "should be ignored" not in titles  # pruned node_modules

    report = coverage_gap(MULTI_LAYER, existing)
    covered_titles = {c["scenario"] for c in report["likely_covered"]}
    assert "Init renders listed chips" in covered_titles
    assert report["gap_count"] >= 1
