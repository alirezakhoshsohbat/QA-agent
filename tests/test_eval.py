from __future__ import annotations

from pathlib import Path

from qa_agent.eval import evaluate_feature, run_eval

REPO_ROOT = Path(__file__).resolve().parents[1]

GOOD = """Feature: Auth
  @acceptance-AC-1
  Scenario: Login ok
    Given POST /api/auth/login with body { "email": "a@b.co" }
    When processed
    Then the response status is 200
"""

BAD = """Feature: Auth
  Scenario: Login
    Given the system is in a valid initial state
    When the user performs the main action
    Then the expected outcome is observed
"""


def test_evaluate_feature_pass():
    result = evaluate_feature(
        GOOD,
        [{"id": "AC-1", "layer": "bff", "summary": "login"}],
        {
            "require_no_blocking": True,
            "require_full_coverage": True,
            "min_scenarios": 1,
            "must_match": ["POST /api/auth/login"],
            "max_quality_warnings": 0,
        },
    )
    assert result["passed"], result["failures"]
    assert result["coverage"]["covered"] == 1


def test_evaluate_feature_fail_on_missing_coverage_and_vague():
    result = evaluate_feature(
        BAD,
        [{"id": "AC-1", "layer": "bff", "summary": "login"}],
        {"require_no_blocking": True, "require_full_coverage": True},
    )
    assert not result["passed"]
    assert result["failures"]


def test_offline_eval_suite_all_pass():
    summary = run_eval(REPO_ROOT / "evals" / "cases.json", live=False)
    assert summary["all_passed"], summary
    assert summary["total"] >= 2
