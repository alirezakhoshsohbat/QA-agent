from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

from qa_agent.cost import CostTracker
from qa_agent.domain import load_domain_rules
from qa_agent.models.schemas import (
    AcceptanceCriterion,
    CoverageReport,
    GeneratedFeature,
    SourceReference,
    slugify,
)

MAX_WRITE_ATTEMPTS = 2

# Generic, domain-neutral vague phrases. Domain-specific vague phrases (if any)
# come from the active domain profile (qa_agent.domain).
VAGUE_PHRASE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("works correctly", re.compile(r"\bworks correctly\b", re.IGNORECASE)),
    ("valid initial state", re.compile(r"\bvalid initial state\b", re.IGNORECASE)),
    ("expected outcome", re.compile(r"\bexpected outcome\b", re.IGNORECASE)),
    ("appropriate error message", re.compile(r"\bappropriate error message\b", re.IGNORECASE)),
    ("performs the main action", re.compile(r"\bperforms the main action\b", re.IGNORECASE)),
    ("handles correctly", re.compile(r"\bhandles correctly\b", re.IGNORECASE)),
    ("updates correctly", re.compile(r"\bupdates correctly\b", re.IGNORECASE)),
]

# Structural signals that make a Gherkin step concrete, independent of any
# product domain (endpoints, HTTP verbs, status codes, JSON/array literals,
# quoted key/value pairs, code-like calls). Domain vocabulary is unioned on top
# via load_domain_rules().concrete_signal_patterns.
GENERIC_CONCRETE_SIGNAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/api/[\w/-]+"),
    re.compile(r"/v\d+/[\w/.-]+"),
    re.compile(r"localStorage\s*(?:\[\s*['\"][^'\"]+['\"]\s*\]|['\"][\w.]+['\"])"),
    re.compile(r"\b[a-z][a-zA-Z0-9]{2,}\([^)]*\)"),  # camelCase calls e.g. sanitizeFields(filters)
    re.compile(r"\{[^}]{2,}\}"),
    re.compile(r"\[[^\]]{2,}\]"),
    re.compile(r"['\"][\w.]+['\"]\s*(?:=|:)"),
    re.compile(r"\b(?:GET|POST|PATCH|PUT|DELETE)\s+/"),
    re.compile(r"\bHTTP status \d+\b"),
    re.compile(r"\b(?:400|401|403|404|409|422|500|502)\b"),
]

ABSTRACT_VERB_PATTERN = re.compile(
    r"\b(?:handles|updates correctly|works as expected)\b",
    re.IGNORECASE,
)

# Conditional logic inside a Then/And assertion (e.g. "if <x> is shown then ...",
# "... otherwise ...") is an anti-pattern: a step must assert one deterministic
# outcome. Branches belong in separate scenarios or Examples rows.
CONDITIONAL_STEP_PATTERN = re.compile(
    r"\bif\b|\botherwise\b|\beither\b.+\bor\b|\bdepending on\b|\bwhen applicable\b",
    re.IGNORECASE,
)

FEATURE_LINE_PATTERN = re.compile(r"^Feature:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

SCENARIO_BLOCK_PATTERN = re.compile(
    r"^\s*Scenario(?:\s+Outline)?:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

DRY_RUN_TEMPLATE = """@dry-run-template
Feature: Dry Run Preview
  As a QA engineer
  I want a concrete placeholder feature file
  So that the pipeline can be tested without LLM calls

  Background:
    Given the dry-run mode is enabled for query "{query}"

  @acceptance-DRY-1 @layer-backend
  Scenario: Example API call with concrete request body
    Given GET /api/health returns status 200
    When the client sends POST /api/items with body {{"name":"example","quantity":1}}
    Then the response status is 201
    And response body contains field "id"
"""


def extract_gherkin_from_text(text: str) -> str:
    """Extract Gherkin block from agent response.

    Keeps any `@tag` lines (e.g. `@layer-frontend`) that sit directly above the
    first `Feature:` so the leading feature does not lose its layer/source tags.
    """
    match = re.search(
        r"((?:^[ \t]*@[^\n]*\n)*Feature:.*)",
        text,
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    if match:
        content = match.group(1).strip()
        content = re.sub(r"```\s*$", "", content).strip()
        return content

    fence_match = re.search(r"```(?:gherkin|feature)?\s*\n((?:[ \t]*@[^\n]*\n)*Feature:.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()

    return text.strip()


def _normalize_feature_title(title: str) -> str:
    normalized = title.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _titles_are_duplicate(first: str, second: str) -> bool:
    left = _normalize_feature_title(first)
    right = _normalize_feature_title(second)
    if left == right:
        return True
    return left.startswith(right) or right.startswith(left)


def _feature_level_layer(block_text: str) -> str | None:
    """Return the `@layer-<name>` declared on the feature's own tag line, if any.

    Only the tags above the `Feature:` line count as the feature layer; scenario
    tags inside the block are ignored so a frontend feature with a stray backend
    scenario tag is not misclassified.
    """
    head = re.split(r"^\s*Feature:", block_text, maxsplit=1, flags=re.IGNORECASE)[0]
    match = re.search(r"@layer-([\w-]+)", head, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _feature_block_ranges(content: str) -> list[dict[str, str | int | None]]:
    matches = list(FEATURE_LINE_PATTERN.finditer(content))
    if not matches:
        return []

    # Extend each feature's start backwards to swallow its leading @tag lines
    # (e.g. `@layer-frontend`) so tags are preserved and layers are detectable.
    starts = [_block_start_with_tags(content, match.start()) for match in matches]

    blocks: list[dict[str, str | int | None]] = []
    for index, match in enumerate(matches):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(content)
        text = content[start:end].strip()
        blocks.append(
            {
                "title": match.group(1).strip(),
                "start": start,
                "end": end,
                "text": text,
                "layer": _feature_level_layer(text),
            }
        )
    return blocks


def _blocks_are_mergeable(
    first: dict[str, str | int | None],
    second: dict[str, str | int | None],
) -> bool:
    """Two features merge only when titles duplicate AND they are the same layer.

    Layer-split output (one Feature per layer, e.g. `Search Pane` frontend and
    `Search Pane Backend` bff) must be preserved — never collapsed — even though
    one title is a prefix of the other.
    """
    if not _titles_are_duplicate(str(first["title"]), str(second["title"])):
        return False
    first_layer = first.get("layer")
    second_layer = second.get("layer")
    if first_layer and second_layer and first_layer != second_layer:
        return False
    return True


def _split_block_preamble_and_scenarios(block_text: str) -> tuple[list[str], list[str]]:
    """Return (preamble_lines, scenario_blocks) for a single Feature block.

    Preamble = user-story / Background / comment lines under `Feature:` (feature
    and scenario @tag lines are dropped from the preamble). Scenario blocks keep
    their own leading @tags so `@acceptance-*` coverage survives the merge.
    """
    scenario_matches = list(SCENARIO_BLOCK_PATTERN.finditer(block_text))
    if scenario_matches:
        scenario_starts = [
            _block_start_with_tags(block_text, match.start()) for match in scenario_matches
        ]
        preamble_text = block_text[: scenario_starts[0]]
        scenario_blocks = [
            block_text[
                scenario_starts[i] : (
                    scenario_starts[i + 1] if i + 1 < len(scenario_starts) else len(block_text)
                )
            ].strip()
            for i in range(len(scenario_matches))
        ]
    else:
        preamble_text = block_text
        scenario_blocks = []

    preamble_lines: list[str] = []
    for line in preamble_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("@"):
            continue
        if re.match(r"^Feature:", stripped, re.IGNORECASE):
            continue
        if stripped.startswith("#"):
            preamble_lines.append(f"  {stripped.lstrip('#').strip()}")
        else:
            preamble_lines.append(f"  {stripped}")

    return preamble_lines, scenario_blocks


def _merge_group(group: list[dict[str, str | int | None]]) -> str:
    kept_title = str(group[-1]["title"])
    preamble_lines: list[str] = []
    seen_preamble: set[str] = set()
    scenario_blocks: list[str] = []

    for block in group:
        block_preamble, block_scenarios = _split_block_preamble_and_scenarios(
            str(block["text"])
        )
        for line in block_preamble:
            key = line.strip().lower()
            if key not in seen_preamble:
                seen_preamble.add(key)
                preamble_lines.append(line)
        scenario_blocks.extend(block_scenarios)

    parts = [f"Feature: {kept_title}", *preamble_lines]
    if scenario_blocks:
        parts.append("")
        parts.append("\n\n".join(scenario_blocks))
    return "\n".join(parts).strip()


def _merge_duplicate_feature_blocks(blocks: list[dict[str, str | int | None]]) -> list[str]:
    if not blocks:
        return []

    merged: list[str] = []
    index = 0
    while index < len(blocks):
        group = [blocks[index]]
        while index + 1 < len(blocks) and _blocks_are_mergeable(
            blocks[index],
            blocks[index + 1],
        ):
            index += 1
            group.append(blocks[index])

        if len(group) == 1:
            merged.append(str(group[0]["text"]))
        else:
            merged.append(_merge_group(group))

        index += 1

    return merged


def normalize_gherkin(content: str) -> str:
    """Extract Gherkin; collapse accidental duplicate Features; preserve distinct multi-Feature files."""
    content = extract_gherkin_from_text(content)
    blocks = _feature_block_ranges(content)
    if len(blocks) <= 1:
        return content

    merged_blocks = _merge_duplicate_feature_blocks(blocks)
    if len(merged_blocks) == 1:
        return merged_blocks[0]

    return "\n\n".join(merged_blocks).strip()


def _last_feature_end(content: str) -> int:
    blocks = _feature_block_ranges(content)
    if not blocks:
        return 0
    return int(blocks[-1]["end"])


def repair_orphan_scenarios(content: str) -> str:
    """Attach Scenario blocks that appear after the last Feature to that Feature."""
    blocks = _feature_block_ranges(content)
    if not blocks:
        return content

    last_end = int(blocks[-1]["end"])
    tail = content[last_end:].strip()
    if not tail or not SCENARIO_BLOCK_PATTERN.search(tail):
        return content

    last_text = str(blocks[-1]["text"]).rstrip()
    repaired = f"{last_text}\n\n{tail}".strip()
    prefix = content[: int(blocks[-1]["start"])].rstrip()
    if prefix:
        earlier = "\n\n".join(str(block["text"]).strip() for block in blocks[:-1]).strip()
        return f"{earlier}\n\n{repaired}".strip()
    return repaired


def prepare_gherkin_for_write(content: str) -> str:
    return repair_orphan_scenarios(normalize_gherkin(content))


def validate_gherkin(content: str) -> list[str]:
    """Basic Gherkin syntax validation."""
    errors: list[str] = []
    if not re.search(r"^Feature:", content, re.MULTILINE | re.IGNORECASE):
        errors.append("Missing Feature: declaration")
    if not re.search(r"^\s*Scenario:", content, re.MULTILINE | re.IGNORECASE):
        errors.append("Missing at least one Scenario:")
    steps = re.findall(r"^\s*(Given|When|Then|And|But)\s+", content, re.MULTILINE | re.IGNORECASE)
    if not steps:
        errors.append("Missing Given/When/Then steps")
    return errors


def _scenario_acceptance_tags(block: str) -> set[str]:
    tags = set(re.findall(r"@acceptance-([\w-]+)", block, re.IGNORECASE))
    tags.update(re.findall(r"@wip-([\w-]+)", block, re.IGNORECASE))
    return tags


def _find_orphan_scenarios(content: str) -> list[str]:
    blocks = _feature_block_ranges(content)
    if not blocks:
        return []

    last_end = int(blocks[-1]["end"])
    tail = content[last_end:]
    orphans: list[str] = []
    for match in SCENARIO_BLOCK_PATTERN.finditer(tail):
        orphans.append(match.group(1).strip())
    return orphans


def validate_gherkin_structure(
    content: str,
    checklist: list[AcceptanceCriterion] | None = None,
) -> list[str]:
    """Blocking structural and PRD contract errors."""
    if _is_dry_run_template(content):
        return []

    errors: list[str] = []

    orphans = _find_orphan_scenarios(content)
    for title in orphans:
        errors.append(f"Orphan scenario outside Feature: '{title}'")

    checklist = checklist or []
    checklist_ids = {item.id for item in checklist}

    for title, block in _split_scenario_blocks(content):
        if not re.search(
            r"^\s*(Given|When|Then|And|But)\s+",
            block,
            re.MULTILINE | re.IGNORECASE,
        ):
            errors.append(f"Scenario '{title}' is missing Given/When/Then steps")

        scenario_tags = _scenario_acceptance_tags(block)
        if checklist_ids and not scenario_tags:
            errors.append(f"Scenario '{title}' is missing @acceptance-{{ID}} or @wip-{{ID}} tag")

        # Multiple scenarios may share the same @acceptance-{ID} on purpose: a
        # single behavior is covered in depth by a happy-path scenario plus edge,
        # failure, race, cross-tab, and rehydration variants. Reusing an ID is
        # therefore encouraged for coverage depth, not an error.

        if checklist_ids and scenario_tags:
            unknown = scenario_tags - checklist_ids
            if unknown:
                errors.append(
                    f"Scenario '{title}' uses unknown checklist IDs: {', '.join(sorted(unknown))}"
                )

    for message, pattern in load_domain_rules().forbidden_patterns:
        if pattern.search(content):
            errors.append(message)

    return errors


def validate_before_write(
    content: str,
    checklist: list[AcceptanceCriterion],
) -> tuple[list[str], list[str], CoverageReport]:
    syntax_errors = validate_gherkin(content)
    structural_errors = validate_gherkin_structure(content, checklist)
    id_scheme_errors = validate_checklist_id_scheme(content, checklist)
    quality_warnings = lint_gherkin_quality(content)
    quality_warnings.extend(lint_checklist_alignment(content, checklist))
    coverage = build_coverage_report(content, checklist)

    blocking = [*syntax_errors, *structural_errors, *id_scheme_errors]
    if checklist and coverage.missing:
        blocking.append(f"Missing acceptance tags for checklist IDs: {', '.join(coverage.missing)}")

    return blocking, quality_warnings, coverage


def format_blocked_message(
    errors: list[str], attempt: int, max_attempts: int = MAX_WRITE_ATTEMPTS
) -> str:
    max_attempts = max(1, max_attempts)
    capped = min(attempt, max_attempts)
    lines = [f"BLOCKED (attempt {capped}/{max_attempts}):"]
    lines.extend(f"- {error}" for error in errors)
    if capped < max_attempts:
        lines.append(
            f"Fix every error above and call write_feature_file again with "
            f"write_attempt={capped + 1}."
        )
    else:
        lines.append("Max write attempts reached. Summarize remaining gaps for the user.")
    return "\n".join(lines)


def is_blocked_result(result: str) -> bool:
    return str(result).strip().startswith("BLOCKED")


def _is_dry_run_template(content: str) -> bool:
    return bool(re.search(r"@dry-run-template\b", content))


def _block_start_with_tags(content: str, scenario_start: int) -> int:
    """Extend a scenario's start backwards to include its leading @tag lines.

    Gherkin tags sit on the line(s) directly above `Scenario:`. Without this, the
    tags would be attributed to the previous scenario and the last scenario would
    always appear untagged.
    """
    cursor = scenario_start
    while cursor > 0:
        prev_line_end = cursor - 1  # the newline terminating the previous line
        prev_line_start = content.rfind("\n", 0, prev_line_end) + 1
        prev_line = content[prev_line_start:prev_line_end]
        if prev_line.strip().startswith("@"):
            cursor = prev_line_start
        else:
            break
    return cursor


def _split_scenario_blocks(content: str) -> list[tuple[str, str]]:
    """Return (title, block_text) for each Scenario / Scenario Outline.

    Each block includes the scenario's own leading @tag lines and extends to the
    start of the next scenario's tags (or end of content).
    """
    matches = list(SCENARIO_BLOCK_PATTERN.finditer(content))
    if not matches:
        return []

    starts = [_block_start_with_tags(content, match.start()) for match in matches]

    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(content)
        title = match.group(1).strip()
        blocks.append((title, content[start:end]))
    return blocks


def _normalize_scenario_title(title: str) -> str:
    normalized = title.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _scenario_has_concrete_signal(block: str) -> bool:
    if any(pattern.search(block) for pattern in GENERIC_CONCRETE_SIGNAL_PATTERNS):
        return True
    return any(pattern.search(block) for pattern in load_domain_rules().concrete_signal_patterns)


def _count_scenarios(content: str) -> int:
    return len(_split_scenario_blocks(content))


def lint_checklist_alignment(content: str, checklist: list[AcceptanceCriterion]) -> list[str]:
    """Non-blocking warnings when scenario count or ID scheme diverges from checklist."""
    if not checklist:
        return []

    warnings: list[str] = []
    scenario_count = _count_scenarios(content)
    checklist_ids = {item.id for item in checklist}
    tagged = parse_acceptance_tags(content) | parse_wip_tags(content)

    if scenario_count < len(checklist):
        warnings.append(
            f"Only {scenario_count} scenarios for {len(checklist)} checklist items — "
            "cover every ID at least once, then add edge/failure/race/cross-tab "
            "variants so strong behaviors get multiple scenarios"
        )

    # Detect catch-all abuse: reusing one @acceptance-{ID} is only valid for
    # different angles of the *same* behavior. If a single ID is stretched across
    # many scenarios (or dominates the whole file), the model is likely funneling
    # unrelated behaviors under one ID instead of adding new checklist rows.
    id_usage: Counter[str] = Counter()
    for _title, block in _split_scenario_blocks(content):
        for tag in _scenario_acceptance_tags(block):
            id_usage[tag] += 1
    if id_usage:
        top_id, top_count = id_usage.most_common(1)[0]
        overloaded = top_count > 6 or (
            scenario_count >= 8 and top_count >= max(5, scenario_count * 0.4)
        )
        if overloaded:
            warnings.append(
                f"@acceptance-{top_id} is used on {top_count} scenarios — reuse an ID "
                "only for different angles of the SAME behavior. Give unrelated "
                "behaviors (FAQ, map, blog, SEO, navigation, …) their own new "
                "checklist IDs instead of overloading one ID"
            )

    prd_ids = {item_id for item_id in checklist_ids if re.match(r"^[EB]-\d+", item_id, re.IGNORECASE)}
    synthetic_in_gherkin = {tag for tag in tagged if re.match(r"^AC-\d+$", tag, re.IGNORECASE)}
    if prd_ids and synthetic_in_gherkin:
        warnings.append(
            "Gherkin uses synthetic AC-* tags but checklist has PRD IDs (E-*, B-*) — "
            "retag scenarios with @acceptance-E-1 / @acceptance-B-8 verbatim"
        )

    return warnings


def validate_checklist_id_scheme(
    content: str,
    checklist: list[AcceptanceCriterion],
) -> list[str]:
    """Blocking errors when PRD IDs were renamed to synthetic AC-* tags."""
    if not checklist:
        return []

    checklist_ids = {item.id for item in checklist}
    prd_ids = {item_id for item_id in checklist_ids if re.match(r"^[EB]-\d+", item_id, re.IGNORECASE)}
    if not prd_ids:
        return []

    tagged = parse_acceptance_tags(content) | parse_wip_tags(content)
    synthetic_in_gherkin = {tag for tag in tagged if re.match(r"^AC-\d+$", tag, re.IGNORECASE)}
    if synthetic_in_gherkin:
        return [
            "Checklist uses PRD IDs (E-*, B-*) but Gherkin tags use synthetic AC-* — "
            "copy doc IDs verbatim and one @acceptance-{ID} per checklist row"
        ]
    return []


def lint_gherkin_quality(content: str) -> list[str]:
    """Non-blocking quality warnings for vague or weak scenarios."""
    if _is_dry_run_template(content):
        return []

    warnings: list[str] = []
    rules = load_domain_rules()

    for phrase_label, pattern in (*VAGUE_PHRASE_PATTERNS, *rules.extra_vague_phrases):
        if pattern.search(content):
            warnings.append(f"Vague phrase detected: '{phrase_label}'")

    scenario_blocks = _split_scenario_blocks(content)
    if scenario_blocks and not parse_acceptance_tags(content) and not parse_wip_tags(content):
        warnings.append(
            "No @acceptance-{ID}/@wip-{ID} tags: scenarios are not mapped to any "
            "acceptance criteria — provide an acceptance checklist and tag each scenario"
        )

    seen_titles: dict[str, str] = {}
    for title, block in scenario_blocks:
        normalized = _normalize_scenario_title(title)
        if normalized in seen_titles:
            warnings.append(
                f"Duplicate scenario intent: '{title}' overlaps '{seen_titles[normalized]}'"
            )
        else:
            seen_titles[normalized] = title

        step_lines = [
            line.strip()
            for line in block.splitlines()
            if re.match(r"^\s*(Given|When|Then|And|But)\s+", line, re.IGNORECASE)
        ]
        if step_lines and not _scenario_has_concrete_signal(block):
            warnings.append(
                f"Scenario '{title}' lacks concrete signals (endpoint, field name, or sample JSON)"
            )

        when_steps = [line for line in step_lines if re.match(r"^When\b", line, re.IGNORECASE)]
        if len(when_steps) > 1:
            warnings.append(
                f"Scenario '{title}' has {len(when_steps)} 'When' steps — a scenario "
                "must have exactly one trigger. Split click-then-click / multi-action "
                "flows into separate single-When scenarios"
            )

        for line in step_lines:
            if re.match(r"^(Then|And|But)\b", line, re.IGNORECASE) and CONDITIONAL_STEP_PATTERN.search(line):
                warnings.append(
                    f"Scenario '{title}' has a conditional assertion: {line[:80]} — "
                    "assert one deterministic outcome; move each branch to its own "
                    "scenario or Examples row"
                )

        domain_context = rules.domain_context_pattern
        for line in step_lines:
            if (
                ABSTRACT_VERB_PATTERN.search(line)
                and not _scenario_has_concrete_signal(line)
                and not (domain_context and domain_context.search(line))
            ):
                warnings.append(f"Scenario '{title}' uses abstract wording: {line[:80]}")

    return warnings


def parse_acceptance_tags(content: str) -> set[str]:
    return set(re.findall(r"@acceptance-([\w-]+)", content, re.IGNORECASE))


def parse_wip_tags(content: str) -> set[str]:
    return set(re.findall(r"@wip-([\w-]+)", content, re.IGNORECASE))


def check_acceptance_coverage(content: str, ac_ids: list[str]) -> list[str]:
    """Return checklist IDs not covered by @acceptance-{ID} or @wip-{ID}."""
    if not ac_ids:
        return []

    covered = parse_acceptance_tags(content)
    wip = parse_wip_tags(content)
    return [ac_id for ac_id in ac_ids if ac_id not in covered and ac_id not in wip]


def build_coverage_report(
    content: str,
    checklist: list[AcceptanceCriterion],
) -> CoverageReport:
    if not checklist:
        return CoverageReport()

    ac_ids = [item.id for item in checklist]
    covered_tags = parse_acceptance_tags(content)
    wip_tags = parse_wip_tags(content)
    missing = check_acceptance_coverage(content, ac_ids)
    covered_count = sum(1 for ac_id in ac_ids if ac_id in covered_tags)

    return CoverageReport(
        checklist_total=len(ac_ids),
        covered=covered_count,
        missing=missing,
        wip=sorted(ac_id for ac_id in ac_ids if ac_id in wip_tags and ac_id not in covered_tags),
    )


def build_feature_meta(
    feature: GeneratedFeature,
    *,
    timestamp: str,
    blocked: bool = False,
    write_attempt: int = 1,
) -> dict:
    validation_errors = validate_gherkin(feature.feature_content)
    quality_warnings = lint_gherkin_quality(feature.feature_content)
    coverage = build_coverage_report(feature.feature_content, feature.acceptance_checklist)

    return {
        "query": feature.query,
        "slug": feature.slug,
        "timestamp": timestamp,
        "sources": [s.model_dump() for s in feature.sources],
        "acceptance_checklist": [c.model_dump() for c in feature.acceptance_checklist],
        "validation_errors": validation_errors,
        "quality_warnings": quality_warnings,
        "coverage": coverage.model_dump(),
        "blocked": blocked,
        "write_attempt": write_attempt,
    }


def resolve_gherkin_from_output(
    *,
    response: str,
    feature_path: Path | None = None,
) -> tuple[str, list[str]]:
    """Prefer saved .feature content over agent prose when validating/displaying."""
    if feature_path and feature_path.exists():
        gherkin = prepare_gherkin_for_write(feature_path.read_text(encoding="utf-8"))
        return gherkin, validate_gherkin(gherkin)

    gherkin = prepare_gherkin_for_write(response)
    return gherkin, validate_gherkin(gherkin)


def write_feature_files(
    feature: GeneratedFeature,
    output_dir: Path,
    cost: CostTracker | None = None,
    model: str = "gpt-4.1",
    *,
    blocked: bool = False,
    write_attempt: int = 1,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_name = f"{feature.slug}-{timestamp}"

    feature_path = output_dir / f"{base_name}.feature"
    meta_path = output_dir / f"{base_name}.meta.json"
    cost_path = output_dir / f"{base_name}.cost.json"

    feature_path.write_text(feature.feature_content, encoding="utf-8")

    meta = build_feature_meta(
        feature,
        timestamp=timestamp,
        blocked=blocked,
        write_attempt=write_attempt,
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if cost:
        cost.save(cost_path, model=model)

    return {"feature": feature_path, "meta": meta_path, "cost": cost_path}


def parse_acceptance_checklist(raw: str) -> list[AcceptanceCriterion]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(items, list):
        return []

    checklist: list[AcceptanceCriterion] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            checklist.append(AcceptanceCriterion(**item))
        except (TypeError, ValueError):
            continue
    return checklist


def try_write_feature_file(
    *,
    gherkin_content: str,
    query: str,
    sources_json: str,
    acceptance_checklist_json: str,
    write_attempt: int,
    output_dir: Path,
    cost: CostTracker | None,
    max_write_attempts: int = MAX_WRITE_ATTEMPTS,
) -> str:
    content = prepare_gherkin_for_write(gherkin_content)
    checklist = parse_acceptance_checklist(acceptance_checklist_json)
    blocking_errors, quality_warnings, coverage = validate_before_write(content, checklist)

    try:
        raw_sources = json.loads(sources_json) if sources_json else []
        sources = [SourceReference(**s) for s in raw_sources]
    except (json.JSONDecodeError, TypeError, ValueError):
        sources = []

    if blocking_errors:
        return format_blocked_message(blocking_errors, write_attempt, max_write_attempts)

    feature = GeneratedFeature(
        slug=slugify(query),
        feature_content=content,
        sources=sources,
        query=query,
        acceptance_checklist=checklist,
    )
    paths = write_feature_files(
        feature,
        output_dir,
        cost,
        write_attempt=write_attempt,
    )
    result = f"Written: {paths['feature']}"
    if quality_warnings:
        result += f"\nQuality warnings ({len(quality_warnings)}): {quality_warnings[0]}"
        if len(quality_warnings) > 1:
            result += f" (+{len(quality_warnings) - 1} more)"
    if checklist:
        result += (
            f"\nCoverage: {coverage.covered}/{coverage.checklist_total} acceptance criteria tagged"
        )
    return result


def create_output_tools(
    output_dir: Path,
    cost: CostTracker | None = None,
    request_query: str | None = None,
    max_write_attempts: int = MAX_WRITE_ATTEMPTS,
) -> list:
    write_attempt_cap = max(1, int(max_write_attempts))

    @tool
    def write_feature_file(
        gherkin_content: str,
        query: str,
        sources_json: str = "[]",
        acceptance_checklist_json: str = "[]",
        write_attempt: int = 1,
    ) -> str:
        """Write Gherkin test cases to a local .feature file with metadata.

        Args:
            gherkin_content: Full Gherkin feature file content starting with Feature:
            query: Original user query/request
            sources_json: JSON array of sources [{kind, id, title, url}]
            acceptance_checklist_json: JSON array [{id, layer, summary}] — copy PRD IDs
                (E-1, B-8) verbatim; one Scenario per row; do not merge or rename to AC-*
            write_attempt: 1 on first try, increment after each BLOCKED response
        """
        attempt = max(1, min(int(write_attempt), write_attempt_cap))
        # Prefer the real user request bound at run time — the model sometimes
        # echoes the wrapper prompt into `query`, producing ugly slugs/metadata.
        effective_query = (request_query or "").strip() or query
        return try_write_feature_file(
            gherkin_content=gherkin_content,
            query=effective_query,
            sources_json=sources_json,
            acceptance_checklist_json=acceptance_checklist_json,
            write_attempt=attempt,
            output_dir=output_dir,
            cost=cost,
            max_write_attempts=write_attempt_cap,
        )

    return [write_feature_file]
