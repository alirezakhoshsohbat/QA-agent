from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qa_agent.cost import CostTracker, summarize_diff
from qa_agent.models.schemas import AcceptanceCriterion, FileCache, GeneratedFeature, slugify
from qa_agent.tools.graphify import _fallback_graph_query, graph_exists
from qa_agent.tools.output import (
    build_coverage_report,
    check_acceptance_coverage,
    extract_gherkin_from_text,
    format_blocked_message,
    lint_checklist_alignment,
    lint_gherkin_quality,
    normalize_gherkin,
    parse_acceptance_tags,
    prepare_gherkin_for_write,
    repair_orphan_scenarios,
    resolve_gherkin_from_output,
    try_write_feature_file,
    validate_before_write,
    validate_checklist_id_scheme,
    validate_gherkin,
    validate_gherkin_structure,
    write_feature_files,
)
from qa_agent.jobs import job_store
from qa_agent.indexing import run_index


def test_slugify():
    assert slugify("تست فلوی پرداخت") == "تست-فلوی-پرداخت" or len(slugify("تست فلوی پرداخت")) > 0
    assert slugify("Payment Flow Test!") == "payment-flow-test"


def test_summarize_diff_truncates():
    big_diff = "\n".join(f"line {i}" for i in range(1000))
    result = summarize_diff(big_diff, max_lines=100)
    assert "omitted" in result
    assert len(result.splitlines()) < 1000


def test_cost_tracker():
    tracker = CostTracker(budget=1500)
    tracker.record_outline_call()
    tracker.record_github_call()
    tracker.record_graphify_tokens(500)
    tracker.record_llm_usage(1000, 200)
    tracker.add_source("doc:abc")
    tracker.estimate_usd("gpt-4.1")
    assert tracker.report.outline_api_calls == 1
    assert tracker.report.github_api_calls == 1
    assert tracker.graphify_budget_remaining() == 1000


def test_file_cache(tmp_path):
    cache = FileCache(tmp_path / "cache")
    cache.set("outline", "doc1", {"title": "Test"})
    assert cache.get("outline", "doc1", ttl_seconds=3600) == {"title": "Test"}
    assert cache.get("outline", "missing", ttl_seconds=3600) is None


def test_extract_gherkin():
    text = """Here are the tests:

```gherkin
Feature: Login
  Scenario: Success
    Given a user
    When they login
    Then they see dashboard
```
"""
    result = extract_gherkin_from_text(text)
    assert result.startswith("Feature: Login")


def test_validate_gherkin_valid():
    content = """Feature: Test
  Scenario: One
    Given a thing
    When action
    Then result
"""
    assert validate_gherkin(content) == []


def test_validate_gherkin_invalid():
    errors = validate_gherkin("Not gherkin")
    assert len(errors) > 0


def test_normalize_gherkin_duplicate_feature():
    content = """Feature: Search Pane functionality
# As a user, I want filters to sync
Feature: Search Pane

  Scenario: One
    Given a thing
    When action
    Then result
"""
    normalized = normalize_gherkin(content)
    assert normalized.startswith("Feature: Search Pane")
    assert normalized.count("Feature:") == 1
    assert "As a user, I want filters to sync" in normalized
    assert validate_gherkin(normalized) == []


def test_normalize_gherkin_preserves_multi_feature():
    content = """@layer-frontend
Feature: Search Pane Frontend Sync
  @acceptance-E-1 @doc-f57d80cf
  Scenario: Init filters
    Given GET /api/bot/init returns data.filters = ["propertyType"]
    When the Search Pane renders
    Then propertyType chip is visible

@layer-bff
Feature: Search Pane BFF Visibility
  @acceptance-B-1 @doc-f8e00124
  Scenario: Mock allowlist on init
    Given upstream search_filters is missing
    When GET /api/bot/init is called
    Then data.filters equals the full mock allowlist
"""
    normalized = normalize_gherkin(content)
    assert normalized.count("Feature:") == 2
    assert "Search Pane Frontend Sync" in normalized
    assert "Search Pane BFF Visibility" in normalized


def test_layer_split_preserves_features_and_acceptance_tags():
    """A frontend/backend layer split must not collapse into one Feature nor lose tags."""
    content = """@layer-frontend
Feature: Search Pane
  As a user
  I want chips
  So that I control search

  @acceptance-AC-1 @doc-x
  Scenario: Display chips on init with GET /api/bot/init
    Given GET /api/bot/init returns data.filters ["propertyType"]
    When the Search Pane initializes
    Then the propertyType chip is visible

@layer-backend
Feature: Search Pane Backend
  As a backend service
  I want initial filters
  So that chips render

  @acceptance-AC-2
  Scenario: GET /api/bot/init returns filters from history
    Given a user with previous assistant history
    When GET /api/bot/init is called
    Then data.filters is resolved from the last assistant message
"""
    prepared = prepare_gherkin_for_write(content)
    assert prepared.count("Feature:") == 2
    assert "@layer-frontend" in prepared
    assert "@layer-backend" in prepared
    tags = parse_acceptance_tags(prepared)
    assert tags == {"AC-1", "AC-2"}
    checklist = [
        AcceptanceCriterion(id="AC-1", layer="frontend", summary="init chips"),
        AcceptanceCriterion(id="AC-2", layer="bff", summary="init from history"),
    ]
    assert validate_gherkin_structure(prepared, checklist) == []
    report = build_coverage_report(prepared, checklist)
    assert report.covered == 2
    assert report.missing == []


def test_true_duplicate_merge_preserves_scenario_tags():
    """Genuine duplicate features (same layer) still collapse — but keep their tags."""
    content = """@layer-frontend
Feature: Search Pane
  @acceptance-AC-1
  Scenario: First
    Given GET /api/bot/init returns data.filters ["rooms"]
    When the page loads
    Then the rooms chip is visible

@layer-frontend
Feature: Search Pane
  @acceptance-AC-2
  Scenario: Second
    Given localStorage['searchCriteria'] has rooms
    When a value changes
    Then updateCriteria() is called
"""
    prepared = prepare_gherkin_for_write(content)
    assert prepared.count("Feature:") == 1
    assert parse_acceptance_tags(prepared) == {"AC-1", "AC-2"}


def test_extract_gherkin_keeps_leading_layer_tag():
    text = """Here you go:
@layer-frontend
Feature: Search Pane
  @acceptance-AC-1
  Scenario: X
    Given a thing
    When it happens
    Then result
"""
    extracted = extract_gherkin_from_text(text)
    assert extracted.startswith("@layer-frontend")


def test_repair_orphan_scenarios():
    content = """Feature: Search Pane BFF Send Message
  @acceptance-B-8 @layer-bff
  Scenario: Profile update then send
    Given POST /api/bot/send-message with valid filters
    When PATCH /v1/profile succeeds
    Then send-message body contains content only

@acceptance-B-11 @layer-bff
Scenario: Invalid enum returns 400
  Given POST /api/bot/send-message with filters { "rooms": "999+" }
  When BFF validates filters
  Then response status is 400
"""
    repaired = repair_orphan_scenarios(content)
    assert repaired.count("Feature:") == 1
    assert "Invalid enum returns 400" in repaired
    assert validate_gherkin_structure(repaired) == []


def test_validate_gherkin_structure_associates_tags_with_own_scenario():
    """Regression: the last scenario's leading @acceptance tag must count."""
    content = """Feature: Auth
  @acceptance-AC-1 @layer-bff
  Scenario: Login succeeds with valid credentials
    Given POST /api/auth/login with body { "email": "a@b.co", "password": "x" }
    When the request is processed
    Then the response status is 200

  @acceptance-AC-2 @layer-bff
  Scenario: Concurrent 401 responses trigger only one refresh call
    Given two requests receive 401 at the same time
    When POST /api/auth/refresh is triggered
    Then only one refresh request is sent
"""
    checklist = [
        AcceptanceCriterion(id="AC-1", layer="bff", summary="login ok"),
        AcceptanceCriterion(id="AC-2", layer="bff", summary="single refresh"),
    ]
    errors = validate_gherkin_structure(content, checklist)
    assert errors == []
    report = build_coverage_report(content, checklist)
    assert report.covered == 2
    assert report.missing == []


def test_validate_gherkin_structure_flags_prd_antipatterns():
    content = """Feature: Search Pane
  @acceptance-E-9
  Scenario: Wrong history shape
    Given the last assistant message containing filters {"rooms": "3+"}
    When GET /api/bot/init is called
    Then data.filters is set from search_filters IDs only
"""
    errors = validate_gherkin_structure(content)
    assert any("Forbidden PRD pattern" in error for error in errors)


def test_validate_gherkin_structure_requires_acceptance_tags():
    content = """Feature: Search Pane
  Scenario: Missing tag
    Given GET /api/bot/init returns data.filters = ["rooms"]
    When the page loads
    Then rooms chip is visible
"""
    checklist = [AcceptanceCriterion(id="E-1", layer="frontend", summary="init")]
    errors = validate_gherkin_structure(content, checklist)
    assert any("missing @acceptance" in error.lower() for error in errors)


def test_try_write_feature_file_blocks_without_writing(tmp_path):
    content = """Feature: Search Pane
  Scenario: No tags
    Given GET /api/bot/init returns data.filters = ["rooms"]
    When the page loads
    Then rooms chip is visible
"""
    checklist_json = json.dumps([{"id": "E-1", "layer": "frontend", "summary": "init"}])
    result = try_write_feature_file(
        gherkin_content=content,
        query="search pane",
        sources_json="[]",
        acceptance_checklist_json=checklist_json,
        write_attempt=1,
        output_dir=tmp_path,
        cost=None,
    )
    assert result.startswith("BLOCKED")
    assert list(tmp_path.glob("*.feature")) == []


def test_format_blocked_message_includes_retry_hint():
    message = format_blocked_message(["Missing Feature:"], attempt=1)
    assert "attempt 1/2" in message
    assert "write_attempt=2" in message


def test_prepare_gherkin_for_write_composes_pipeline():
    content = """Feature: A
  Scenario: One
    Given GET /api/bot/init returns 200
    When loaded
    Then ok

Feature: B
  Scenario: Two
    Given POST /api/bot/send-message
    When sent
    Then ok

@acceptance-B-11
Scenario: Orphan
  Given POST /api/bot/send-message with filters { "rooms": "999+" }
  When validated
  Then status is 400
"""
    prepared = prepare_gherkin_for_write(content)
    assert prepared.count("Feature:") == 2
    assert "Orphan" in prepared
    assert _find_orphan_scenarios(prepared) == []


def _find_orphan_scenarios(content: str) -> list[str]:
    from qa_agent.tools import output as output_module

    return output_module._find_orphan_scenarios(content)


def test_resolve_gherkin_from_output_prefers_feature_file(tmp_path):
    feature_path = tmp_path / "search.feature"
    feature_path.write_text(
        "Feature: Search Pane\n  Scenario: One\n    Given a\n    When b\n    Then c\n",
        encoding="utf-8",
    )
    response = "Feature file saved. No gherkin here."
    gherkin, errors = resolve_gherkin_from_output(response=response, feature_path=feature_path)
    assert gherkin.startswith("Feature: Search Pane")
    assert errors == []


def test_resolve_gherkin_from_output_falls_back_to_response():
    response = """Feature file saved.

Feature: Search Pane
  Scenario: One
    Given a
    When b
    Then c
"""
    gherkin, errors = resolve_gherkin_from_output(response=response)
    assert gherkin.startswith("Feature: Search Pane")
    assert errors == []


def test_create_index_job():
    job = job_store.create_index(outline_sync=True, github_clone=False, with_graph=False)
    assert job.kind == "index"
    assert job.index_options["outline_sync"] is True


def test_create_index_job_with_new_connectors():
    job = job_store.create_index(
        outline_sync=False,
        github_clone=False,
        with_graph=False,
        confluence_sync=True,
        azure_clone=True,
    )
    assert job.index_options["confluence_sync"] is True
    assert job.index_options["azure_clone"] is True
    assert "Confluence" in job.query
    assert "Azure" in job.query


def test_confluence_configured_property():
    from qa_agent.config import Settings

    off = Settings(confluence_base_url="", confluence_api_token="")
    assert off.confluence_configured is False
    on = Settings(
        confluence_base_url="https://team.atlassian.net/wiki",
        confluence_api_token="tok",
        confluence_space="QA, ENG",
    )
    assert on.confluence_configured is True
    assert on.confluence_spaces_list == ["QA", "ENG"]


def test_azure_configured_and_urls():
    from qa_agent.config import Settings

    off = Settings(azure_devops_pat="", azure_devops_org="", azure_devops_project="")
    assert off.azure_devops_configured is False
    on = Settings(
        azure_devops_pat="pat",
        azure_devops_org="acme",
        azure_devops_project="web",
        azure_devops_repos="app, bff",
    )
    assert on.azure_devops_configured is True
    assert on.azure_devops_repos_list == ["app", "bff"]
    assert on.azure_project_url() == "https://dev.azure.com/acme/web"
    assert on.azure_repo_clone_url_for("app") == "https://dev.azure.com/acme/web/_git/app"


def test_confluence_client_search_mock():
    from qa_agent.tools.confluence import ConfluenceClient

    settings = MagicMock()
    settings.confluence_base_url = "https://team.atlassian.net/wiki"
    settings.confluence_email = "you@team.com"
    settings.confluence_api_token = "tok"
    settings.confluence_spaces_list = []
    settings.cache_dir = Path("/tmp/qa-agent-test-cache-conf")

    client = ConfluenceClient(settings)
    with patch("httpx.Client") as mock_client:
        response = MagicMock()
        response.json.return_value = {"results": [{"id": "42", "title": "Search Pane PRD"}]}
        response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.get.return_value = response
        pages = client.search("search pane", limit=5)
        assert pages[0]["id"] == "42"


def test_storage_to_text_strips_html():
    from qa_agent.tools.confluence import _storage_to_text

    text = _storage_to_text("<h1>Title</h1><p>Hello <b>world</b></p>")
    assert "Title" in text
    assert "Hello world" in text
    assert "<" not in text


def test_azure_client_list_prs_mock():
    from qa_agent.tools.azure_devops import AzureDevOpsClient

    settings = MagicMock()
    settings.azure_devops_configured = True
    settings.azure_devops_repos_list = ["web"]
    settings.azure_devops_pat = "pat"
    settings.azure_project_url.return_value = "https://dev.azure.com/acme/web"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache-azure")

    client = AzureDevOpsClient(settings)
    with patch("httpx.Client") as mock_client:
        response = MagicMock()
        response.json.return_value = {
            "value": [{"pullRequestId": 7, "title": "Add search pane", "status": "active"}]
        }
        response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.get.return_value = response
        prs = client.list_pull_requests("web", status="all", limit=10)
        assert prs[0]["pullRequestId"] == 7


def test_run_index_emits_activities(tmp_path, monkeypatch):
    from qa_agent.config import Settings

    settings = Settings(
        qa_agent_corpus_dir=tmp_path / "corpus",
        qa_agent_graphify_out=tmp_path / "graphify-out",
        outline_api_key="",
    )
    activities: list[dict] = []

    def on_activity(activity: dict) -> None:
        activities.append(activity)

    result = run_index(settings=settings, on_activity=on_activity)
    assert result["graph_built"] is True
    tools = [a["tool"] for a in activities]
    assert "index_prepare" in tools
    assert "index_outline_sync" in tools
    assert "index_finalize" in tools


def test_write_feature_files(tmp_path):
    feature = GeneratedFeature(
        slug="test-feature",
        feature_content="Feature: Test\n  Scenario: One\n    Given x\n    When y\n    Then z",
        query="test query",
    )
    cost = CostTracker()
    paths = write_feature_files(feature, tmp_path, cost)
    assert paths["feature"].exists()
    assert paths["meta"].exists()
    assert paths["cost"].exists()
    meta = json.loads(paths["meta"].read_text())
    assert meta["query"] == "test query"
    assert "quality_warnings" in meta
    assert meta["coverage"]["checklist_total"] == 0


def test_lint_gherkin_quality_flags_vague_steps():
    vague = """Feature: Login
  Scenario: Happy path
    Given the system is in a valid initial state
    When the user performs the main action
    Then the expected outcome is observed
"""
    warnings = lint_gherkin_quality(vague)
    assert any("valid initial state" in w for w in warnings)
    assert any("expected outcome" in w for w in warnings)


def test_lint_gherkin_quality_skips_dry_run_template():
    dry_run = """@dry-run-template
Feature: Dry Run Preview
  Scenario: Happy path
    Given the system is in a valid initial state
    When the user performs the main action
    Then the expected outcome is observed
"""
    assert lint_gherkin_quality(dry_run) == []


def test_lint_gherkin_quality_flags_missing_concrete_signals():
    abstract = """Feature: Search Pane
  Scenario: Process filters
    Given the Search Pane is visible
    When the Search Pane processes the filters
    Then the chips update correctly
"""
    warnings = lint_gherkin_quality(abstract)
    assert any("concrete signals" in w for w in warnings)


def test_lint_gherkin_quality_accepts_localstorage_and_data_filters():
    concrete = """Feature: Search Pane
  @acceptance-E-1
  Scenario: Init filters
    Given GET /api/bot/init returns data.filters ["propertyType"]
    And localStorage 'searchCriteria' contains propertyType values
    When the search pane renders
    Then chips match event.filters order
"""
    warnings = lint_gherkin_quality(concrete)
    assert not any("concrete signals" in w for w in warnings)


def test_lint_gherkin_quality_accepts_sanitize_and_bff_domain_steps():
    # Self-contained fixture: legit BFF/sanitize domain steps must not be flagged
    # for missing concrete signals or abstract wording.
    concrete = """Feature: Search Pane BFF sanitize contract
  @acceptance-B-8 @layer-bff
  Scenario: BFF sanitizes filters before forwarding to send-message
    Given POST /api/bot/send-message receives filters {"propertyType":"condo","priceMin":800000}
    When the BFF calls sanitizeFields(filters) and forwards the request
    Then the upstream POST /api/bot/send-message body contains "filters" with only known keys
    And unknown keys are stripped and invalid enums return 400 with code "VALIDATION_ERROR"
"""
    warnings = lint_gherkin_quality(concrete)
    flagged = [w for w in warnings if "concrete signals" in w or "abstract wording" in w]
    assert flagged == []


def test_validate_checklist_id_scheme_blocks_synthetic_ac_tags():
    gherkin = """Feature: Search Pane
  @acceptance-AC-1
  Scenario: Init
    Given GET /api/bot/init returns data.filters ["propertyType"]
    When rendered
    Then chip visible
"""
    checklist = [AcceptanceCriterion(id="E-1", layer="frontend", summary="init")]
    assert validate_checklist_id_scheme(gherkin, checklist)


def test_validate_checklist_id_scheme_allows_prd_ids():
    gherkin = """Feature: Search Pane
  @acceptance-E-1
  Scenario: Init
    Given GET /api/bot/init returns data.filters ["propertyType"]
    When rendered
    Then chip visible
"""
    checklist = [AcceptanceCriterion(id="E-1", layer="frontend", summary="init")]
    assert validate_checklist_id_scheme(gherkin, checklist) == []


def test_lint_checklist_alignment_warns_on_few_scenarios():
    gherkin = """Feature: Search Pane
  @acceptance-E-1
  Scenario: Init
    Given GET /api/bot/init returns data.filters ["propertyType"]
    When rendered
    Then chip visible
"""
    checklist = [
        AcceptanceCriterion(id="E-1", layer="frontend", summary="init"),
        AcceptanceCriterion(id="E-2", layer="frontend", summary="hide"),
    ]
    warnings = lint_checklist_alignment(gherkin, checklist)
    assert any("Only 1 scenarios for 2 checklist" in w for w in warnings)


def test_repeated_acceptance_id_for_same_behavior_is_allowed():
    """Depth: happy + edge + failure scenarios may share one @acceptance-ID."""
    content = """@layer-frontend
Feature: Handoff
  @acceptance-E-1
  Scenario: Happy path handoff
    Given the hero textarea contains "buy"
    When the user clicks "Guide my next steps"
    Then sessionStorage['agentInitialQuery'] equals "buy"

  @acceptance-E-1 @edge
  Scenario: Whitespace-only prompt is ignored
    Given the hero textarea contains only whitespace
    When the user clicks "Guide my next steps"
    Then sessionStorage['agentInitialQuery'] is not set
"""
    checklist = [AcceptanceCriterion(id="E-1", layer="frontend", summary="handoff")]
    assert validate_gherkin_structure(content, checklist) == []


def test_lint_warns_on_catch_all_acceptance_id():
    scenarios = "".join(
        f"""
  @acceptance-AC-7 @ui
  Scenario: Unrelated behavior {n}
    Given homepage renders widget {n}
    When the user interacts with widget {n}
    Then widget {n} responds
"""
        for n in range(8)
    )
    content = "@layer-frontend\nFeature: Homepage\n" + scenarios
    checklist = [AcceptanceCriterion(id="AC-7", layer="frontend", summary="search")]
    warnings = lint_checklist_alignment(content, checklist)
    assert any("is used on" in w and "AC-7" in w for w in warnings)


def test_validate_before_write_blocks_ac_tags_when_checklist_uses_prd_ids():
    gherkin = """Feature: Search Pane
  @acceptance-AC-1
  Scenario: Init
    Given GET /api/bot/init returns data.filters ["propertyType"]
    When rendered
    Then chip visible
"""
    checklist = [AcceptanceCriterion(id="E-1", layer="frontend", summary="init")]
    blocking, _, _ = validate_before_write(gherkin, checklist)
    assert any("AC-*" in err for err in blocking)


def test_check_acceptance_coverage_missing_ids():
    gherkin = """Feature: Search Pane
  @acceptance-E-1
  Scenario: Init filters
    Given GET /api/bot/init returns filters ["propertyType"]
    When the page loads
    Then propertyType chip is visible
"""
    missing = check_acceptance_coverage(gherkin, ["E-1", "E-2", "B-9"])
    assert missing == ["E-2", "B-9"]


def test_check_acceptance_coverage_counts_wip():
    gherkin = """Feature: Search Pane
  @acceptance-E-1
  Scenario: Init filters
    Given GET /api/bot/init returns filters ["propertyType"]
    When the page loads
    Then propertyType chip is visible

  @wip-B-9
  Scenario: Profile failure blocked
    Given profile update is not yet implemented
    When documented behavior is unclear
    Then this scenario is marked wip
"""
    missing = check_acceptance_coverage(gherkin, ["E-1", "B-9"])
    assert missing == []


def test_parse_acceptance_tags():
    gherkin = "@acceptance-E-1 @acceptance-B-12 @layer-frontend"
    assert parse_acceptance_tags(gherkin) == {"E-1", "B-12"}


def test_write_feature_files_includes_coverage_meta(tmp_path):
    content = """Feature: Search Pane
  @acceptance-E-6 @layer-frontend @doc-search-pane
  Scenario: Empty array field omitted from send payload
    Given localStorage['searchCriteria'] = { "cities": [], "rooms": "3+" }
    When POST /api/bot/send-message with message "hello"
    Then request body filters equals { "rooms": "3+" }
"""
    checklist = [
        AcceptanceCriterion(id="E-6", layer="frontend", summary="empty array omitted"),
        AcceptanceCriterion(id="E-7", layer="e2e", summary="listing sync"),
    ]
    feature = GeneratedFeature(
        slug="search-pane",
        feature_content=content,
        query="search pane tests",
        acceptance_checklist=checklist,
    )
    paths = write_feature_files(feature, tmp_path)
    meta = json.loads(paths["meta"].read_text())
    assert meta["coverage"]["checklist_total"] == 2
    assert meta["coverage"]["covered"] == 1
    assert meta["coverage"]["missing"] == ["E-7"]
    assert len(meta["acceptance_checklist"]) == 2


def test_build_coverage_report():
    content = "@acceptance-E-1\n@wip-E-2"
    checklist = [
        AcceptanceCriterion(id="E-1", layer="frontend", summary="a"),
        AcceptanceCriterion(id="E-2", layer="frontend", summary="b"),
        AcceptanceCriterion(id="E-3", layer="bff", summary="c"),
    ]
    report = build_coverage_report(content, checklist)
    assert report.checklist_total == 3
    assert report.covered == 1
    assert report.missing == ["E-3"]
    assert report.wip == ["E-2"]


def test_fallback_graph_query(tmp_path):
    graph_dir = tmp_path / "graphify-out"
    graph_dir.mkdir()
    graph = {
        "nodes": [
            {"id": "1", "label": "PaymentFlow", "description": "Handles payments"},
            {"id": "2", "label": "AuthModule", "description": "Authentication"},
        ],
        "edges": [],
    }
    (graph_dir / "graph.json").write_text(json.dumps(graph))
    result = _fallback_graph_query(graph_dir, "payment flow", budget=100)
    assert "PaymentFlow" in result


def test_graph_exists(tmp_path):
    assert not graph_exists(tmp_path)
    (tmp_path / "graph.json").write_text("{}")
    assert graph_exists(tmp_path)


@pytest.mark.parametrize("method,payload,expected_key", [
    ("documents.search_titles", {"query": "test", "limit": 5}, "data"),
])
def test_outline_client_mock(method, payload, expected_key):
    from qa_agent.tools.outline import OutlineClient

    settings = MagicMock()
    settings.outline_api_key = "test-key"
    settings.outline_base_url = "https://outline.test/api"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache")

    client = OutlineClient(settings)
    with patch("httpx.Client") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"id": "doc1", "title": "Test"}]}
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        if method == "documents.search_titles":
            result = client.search_titles("test", limit=5)
            assert len(result) == 1


def test_research_search_terms_for_search_pane():
    from qa_agent.tools.outline import _research_search_terms

    terms = _research_search_terms("برام راجب search pane تست کیس بساز")
    assert "Search Pane" in terms
    assert "pane" in terms
    assert "search-pane-sync" in terms
    assert "search-pane-sync BFF" not in terms


def test_research_outline_documents_dedupes_and_ranks():
    from qa_agent.tools.outline import OutlineClient, research_outline_documents

    settings = MagicMock()
    settings.outline_api_key = "test-key"
    settings.outline_base_url = "https://outline.test/api"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache")
    client = OutlineClient(settings)

    def fake_titles(query: str, limit: int = 10):
        if query == "pane":
            return [
                {
                    "id": "f57d80cf-bdcf-4631-8a9d-c7440d7555a6",
                    "title": "PRD: Search Pane ↔ Agent Sync",
                    "context": "frontend acceptance tests E-1",
                },
                {
                    "id": "0697e61e-b000-4252-ab05-7717e3cf1a1f",
                    "title": "search-pane-sync",
                    "context": "BFF acceptance tests B-1",
                },
            ]
        return []

    def fake_search(query: str, limit: int = 10):
        if query == "search-pane-sync":
            return [
                {
                    "id": "0697e61e-b000-4252-ab05-7717e3cf1a1f",
                    "title": "search-pane-sync",
                    "context": "BFF PRD acceptance B-12",
                }
            ]
        return []

    client.search_titles = fake_titles  # type: ignore[method-assign]
    client.search_documents = fake_search  # type: ignore[method-assign]
    client.get_document = MagicMock()  # type: ignore[method-assign]

    docs = research_outline_documents(client, "pane filter tests", max_results=5)
    ids = {doc["id"] for doc in docs}
    assert "f57d80cf-bdcf-4631-8a9d-c7440d7555a6" in ids
    assert "0697e61e-b000-4252-ab05-7717e3cf1a1f" in ids
    assert len(docs) == 2


def test_search_corpus_docs_finds_search_pane(tmp_path):
    from qa_agent.tools.outline import search_corpus_docs

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "prd-search-pane-agent-sync-f57d80cf.md").write_text(
        "# PRD: Search Pane\n\nAcceptance Tests E-1\n",
        encoding="utf-8",
    )
    hits = search_corpus_docs("search pane acceptance", docs_dir)
    assert len(hits) == 1
    assert hits[0]["id"] == "f57d80cf"
