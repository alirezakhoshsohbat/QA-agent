"""Post-generation analysis: traceability matrix and coverage-gap reporting.

All functions here are deterministic (no LLM calls) so they are cheap and safe to
run repeatedly in CI or the web UI.
"""

from __future__ import annotations

import csv
import html
import io
import re
from pathlib import Path
from typing import Any

from qa_agent.models.schemas import AcceptanceCriterion
from qa_agent.tools.output import build_coverage_report

_TAG_RE = re.compile(r"@([\w-]+)")
_FEATURE_RE = re.compile(r"^\s*Feature:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SCENARIO_RE = re.compile(r"^\s*Scenario(?:\s+Outline)?:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Directories that never contain hand-written product tests worth comparing to.
_PRUNE_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__", ".next"}

# `it("...")`, `test('...')`, `describe(\`...\`)` in JS/TS test files.
_JS_TITLE_RE = re.compile(r"""\b(?:it|test|describe)\s*\(\s*(['"`])(.+?)\1""")
_PYTEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)


def normalize_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _significant_tokens(text: str) -> set[str]:
    return {tok for tok in normalize_title(text).split() if len(tok) > 2}


def _strip_prefix(tag: str, prefix: str) -> str | None:
    if tag.lower().startswith(prefix):
        return tag[len(prefix):]
    return None


def parse_scenarios(content: str) -> list[dict[str, Any]]:
    """Parse scenarios with their tags. Tags sit on the line(s) directly above."""
    scenarios: list[dict[str, Any]] = []
    current_feature = ""
    feature_tags: list[str] = []
    pending: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            pending = []
            continue
        if stripped.startswith("@"):
            pending.extend(_TAG_RE.findall(stripped))
            continue

        feature_match = _FEATURE_RE.match(stripped)
        if feature_match:
            current_feature = feature_match.group(1).strip()
            feature_tags = pending
            pending = []
            continue

        scenario_match = _SCENARIO_RE.match(stripped)
        if scenario_match:
            tags = pending
            acceptance = [t[len("acceptance-"):] for t in tags if t.lower().startswith("acceptance-")]
            wip = [t[len("wip-"):] for t in tags if t.lower().startswith("wip-")]
            docs = [t[len("doc-"):] for t in tags if t.lower().startswith("doc-")]
            prs = [t[len("pr-"):] for t in tags if t.lower().startswith("pr-")]
            layers = [
                t[len("layer-"):]
                for t in (tags + feature_tags)
                if t.lower().startswith("layer-")
            ]
            scenarios.append(
                {
                    "title": scenario_match.group(1).strip(),
                    "feature": current_feature,
                    "acceptance": acceptance,
                    "wip": wip,
                    "docs": docs,
                    "prs": prs,
                    "layers": layers,
                }
            )
            pending = []
            continue

        # A step or description line: any pending tags did not belong to a scenario.
        pending = []

    return scenarios


def _coerce_checklist(checklist: list[Any]) -> list[AcceptanceCriterion]:
    result: list[AcceptanceCriterion] = []
    for item in checklist or []:
        if isinstance(item, AcceptanceCriterion):
            result.append(item)
        elif isinstance(item, dict) and item.get("id"):
            try:
                result.append(AcceptanceCriterion(**item))
            except (TypeError, ValueError):
                continue
    return result


def build_traceability(
    content: str,
    checklist: list[Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return one row per acceptance criterion mapping it to scenarios and status."""
    checklist_items = _coerce_checklist(checklist or [])
    scenarios = parse_scenarios(content)

    covered_by: dict[str, list[str]] = {}
    wip_by: dict[str, list[str]] = {}
    docs_by: dict[str, set[str]] = {}
    prs_by: dict[str, set[str]] = {}
    for scenario in scenarios:
        for acc_id in scenario["acceptance"]:
            covered_by.setdefault(acc_id, []).append(scenario["title"])
            docs_by.setdefault(acc_id, set()).update(scenario["docs"])
            prs_by.setdefault(acc_id, set()).update(scenario["prs"])
        for acc_id in scenario["wip"]:
            wip_by.setdefault(acc_id, []).append(scenario["title"])

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in checklist_items:
        covered = covered_by.get(item.id, [])
        wip = wip_by.get(item.id, [])
        status = "covered" if covered else ("wip" if wip else "missing")
        rows.append(
            {
                "id": item.id,
                "layer": item.layer,
                "summary": item.summary,
                "status": status,
                "scenarios": covered or wip,
                "docs": sorted(docs_by.get(item.id, set())),
                "prs": sorted(prs_by.get(item.id, set())),
            }
        )
        seen.add(item.id)

    for acc_id in sorted(set(covered_by) | set(wip_by)):
        if acc_id in seen:
            continue
        covered = covered_by.get(acc_id, [])
        wip = wip_by.get(acc_id, [])
        rows.append(
            {
                "id": acc_id,
                "layer": "",
                "summary": "(tagged but not in checklist)",
                "status": "covered" if covered else "wip",
                "scenarios": covered or wip,
                "docs": sorted(docs_by.get(acc_id, set())),
                "prs": sorted(prs_by.get(acc_id, set())),
            }
        )

    return rows


def traceability_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Acceptance ID", "Layer", "Status", "Summary", "Scenarios", "Docs", "PRs"])
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row.get("layer", ""),
                row["status"],
                row.get("summary", ""),
                " | ".join(row.get("scenarios", [])),
                ", ".join(row.get("docs", [])),
                ", ".join(row.get("prs", [])),
            ]
        )
    return buffer.getvalue()


def traceability_html(rows: list[dict[str, Any]], title: str = "Traceability Matrix") -> str:
    status_color = {"covered": "#16a34a", "wip": "#d97706", "missing": "#dc2626"}
    covered = sum(1 for r in rows if r["status"] == "covered")
    body_rows = []
    for row in rows:
        scenarios = "<br>".join(html.escape(s) for s in row.get("scenarios", [])) or "—"
        color = status_color.get(row["status"], "#334155")
        body_rows.append(
            "<tr>"
            f"<td><code>{html.escape(row['id'])}</code></td>"
            f"<td>{html.escape(row.get('layer', ''))}</td>"
            f"<td style='color:{color};font-weight:600'>{row['status']}</td>"
            f"<td>{html.escape(row.get('summary', ''))}</td>"
            f"<td>{scenarios}</td>"
            f"<td>{html.escape(', '.join(row.get('docs', [])))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #0f172a; }}
  h1 {{ font-size: 1.25rem; }}
  .summary {{ margin-bottom: 1rem; color: #475569; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e2e8f0; padding: .5rem .75rem; text-align: left; vertical-align: top; }}
  th {{ background: #f1f5f9; }}
  code {{ background: #f1f5f9; padding: .1rem .35rem; border-radius: .25rem; }}
</style></head>
<body>
<h1>{html.escape(title)}</h1>
<div class="summary">{covered}/{len(rows)} criteria covered</div>
<table>
<thead><tr><th>ID</th><th>Layer</th><th>Status</th><th>Summary</th><th>Scenarios</th><th>Docs</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table>
</body></html>
"""


def scan_existing_tests(root: Path) -> list[dict[str, str]]:
    """Collect existing test titles from .feature, JS/TS specs, and pytest files."""
    root = Path(root)
    titles: list[dict[str, str]] = []
    if not root.exists():
        return titles

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _PRUNE_DIRS for part in path.parts):
            continue

        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if suffix == ".feature":
            for match in _SCENARIO_RE.finditer(text):
                titles.append({"title": match.group(1).strip(), "path": str(path), "kind": "feature"})
        elif suffix in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}:
            if not re.search(r"\.(?:spec|test)\.|__tests__|/tests?/", str(path)) and "test" not in path.name.lower():
                # Only scan files that look like tests to avoid false matches.
                if not _JS_TITLE_RE.search(text):
                    continue
            for match in _JS_TITLE_RE.finditer(text):
                titles.append({"title": match.group(2).strip(), "path": str(path), "kind": "js"})
        elif suffix == ".py" and ("test" in path.name.lower()):
            for match in _PYTEST_DEF_RE.finditer(text):
                readable = match.group(1)[len("test_"):].replace("_", " ")
                titles.append({"title": readable, "path": str(path), "kind": "pytest"})

    return titles


def coverage_gap(
    content: str,
    existing: list[dict[str, str]],
    *,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Compare generated scenarios against existing tests by title token overlap."""
    scenarios = parse_scenarios(content)
    existing_tokens = [(_significant_tokens(item["title"]), item) for item in existing]

    new: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []

    for scenario in scenarios:
        scenario_tokens = _significant_tokens(scenario["title"])
        best_score = 0.0
        best_item: dict[str, str] | None = None
        for tokens, item in existing_tokens:
            if not scenario_tokens or not tokens:
                continue
            union = len(scenario_tokens | tokens)
            score = len(scenario_tokens & tokens) / union if union else 0.0
            if score > best_score:
                best_score = score
                best_item = item

        entry = {
            "scenario": scenario["title"],
            "acceptance": scenario["acceptance"],
            "best_match": best_item["title"] if best_item else None,
            "match_path": best_item["path"] if best_item else None,
            "score": round(best_score, 2),
        }
        if best_score >= threshold:
            already.append(entry)
        else:
            new.append(entry)

    return {
        "total_scenarios": len(scenarios),
        "existing_tests_scanned": len(existing),
        "new_scenarios": new,
        "likely_covered": already,
        "gap_count": len(new),
    }


def coverage_report_for(content: str, checklist: list[Any] | None) -> dict[str, Any]:
    """Convenience wrapper returning the acceptance coverage report as a dict."""
    report = build_coverage_report(content, _coerce_checklist(checklist or []))
    return report.model_dump()
