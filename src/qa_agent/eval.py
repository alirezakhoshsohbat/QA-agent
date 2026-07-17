"""Quality-gate evaluation harness for generated Gherkin.

Two modes:
- **offline** (default): score existing `.feature` fixtures against expectations
  using the deterministic validators. Cheap, no LLM — ideal for CI regression.
- **live** (`--live`): actually run the agent on a query, then score the newest
  feature file it produces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qa_agent.models.schemas import AcceptanceCriterion
from qa_agent.tools.output import (
    SCENARIO_BLOCK_PATTERN,
    build_coverage_report,
    lint_gherkin_quality,
    validate_gherkin,
    validate_gherkin_structure,
)


def _count_scenarios(content: str) -> int:
    return len(SCENARIO_BLOCK_PATTERN.findall(content))


def evaluate_feature(
    content: str,
    checklist: list[dict[str, Any]] | None,
    expect: dict[str, Any],
) -> dict[str, Any]:
    """Score one feature file against a case's expectations."""
    checklist_items = [
        AcceptanceCriterion(**c) for c in (checklist or []) if isinstance(c, dict) and c.get("id")
    ]

    blocking = [*validate_gherkin(content), *validate_gherkin_structure(content, checklist_items)]
    coverage = build_coverage_report(content, checklist_items)
    if checklist_items and coverage.missing:
        blocking.append(f"missing acceptance coverage: {', '.join(coverage.missing)}")
    warnings = lint_gherkin_quality(content)
    scenarios = _count_scenarios(content)

    failures: list[str] = []

    if expect.get("require_no_blocking", True) and blocking:
        failures.append(f"blocking validation errors: {blocking}")

    min_scenarios = expect.get("min_scenarios")
    if min_scenarios is not None and scenarios < min_scenarios:
        failures.append(f"scenarios {scenarios} < min {min_scenarios}")

    min_criteria = expect.get("min_criteria")
    if min_criteria is not None and len(checklist_items) < min_criteria:
        failures.append(f"criteria {len(checklist_items)} < min {min_criteria}")

    if expect.get("require_full_coverage") and checklist_items and coverage.missing:
        failures.append(f"coverage not full: missing {coverage.missing}")

    for pattern in expect.get("must_match", []):
        if not re.search(pattern, content):
            failures.append(f"missing required pattern: {pattern!r}")

    for pattern in expect.get("must_not_match", []):
        if re.search(pattern, content):
            failures.append(f"forbidden pattern present: {pattern!r}")

    max_warnings = expect.get("max_quality_warnings")
    if max_warnings is not None and len(warnings) > max_warnings:
        failures.append(f"quality warnings {len(warnings)} > max {max_warnings}")

    return {
        "passed": not failures,
        "failures": failures,
        "scenarios": scenarios,
        "criteria": len(checklist_items),
        "coverage": coverage.model_dump(),
        "blocking": blocking,
        "warnings": warnings,
    }


def load_cases(cases_path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases", [])
    return data


def run_eval(
    cases_path: Path,
    *,
    live: bool = False,
    settings: Any | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    """Run all cases and return an aggregate summary."""
    cases_path = Path(cases_path)
    cases = load_cases(cases_path)
    base_dir = cases_path.parent
    results: list[dict[str, Any]] = []

    for case in cases:
        name = case.get("name", "unnamed")
        expect = case.get("expect", {})
        checklist = case.get("checklist", [])

        if case.get("query") and live:
            from qa_agent.agent import run_generate
            from qa_agent.config import get_settings

            active = settings or get_settings()
            output = run_generate(case["query"], settings=active, budget=budget)
            feature_files = sorted(
                active.qa_agent_output_dir.glob("*.feature"),
                key=lambda path: path.stat().st_mtime,
            )
            content = feature_files[-1].read_text(encoding="utf-8") if feature_files else ""
        elif case.get("feature"):
            feature_path = (base_dir / case["feature"]).resolve()
            content = feature_path.read_text(encoding="utf-8") if feature_path.exists() else ""
            if not content:
                results.append({"name": name, "passed": False, "failures": [f"fixture not found: {feature_path}"]})
                continue
        else:
            # A live-only case skipped in offline mode.
            results.append({"name": name, "passed": True, "skipped": True, "failures": []})
            continue

        score = evaluate_feature(content, checklist, expect)
        score["name"] = name
        results.append(score)

    graded = [r for r in results if not r.get("skipped")]
    passed = sum(1 for r in graded if r["passed"])
    return {
        "total": len(graded),
        "passed": passed,
        "failed": len(graded) - passed,
        "all_passed": passed == len(graded),
        "results": results,
    }
