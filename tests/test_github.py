from pathlib import Path
from unittest.mock import MagicMock, patch

from qa_agent.tools.github import (
    GitHubClient,
    _normalize_pr_query,
    _path_is_false_positive,
    _refresh_existing_clone,
    _query_keywords,
    _score_pr_relevance,
    discover_relevant_pull_requests,
    format_pr_summary,
)


def test_format_pr_summary():
    pr = {
        "number": 42,
        "title": "Add payment flow",
        "state": "open",
        "user": {"login": "dev1"},
        "head": {"ref": "feature/pay"},
        "base": {"ref": "main"},
        "html_url": "https://github.com/o/r/pull/42",
        "body": "Implements payment",
    }
    text = format_pr_summary(pr)
    assert "PR #42" in text
    assert "payment flow" in text


def test_query_keywords():
    kws = _query_keywords("راجب search pane برام تست کیس بساز")
    assert "search" in kws
    assert "pane" in kws
    assert "بساز" not in kws


def test_normalize_pr_query_from_long_delegation():
    blob = (
        "Analyze the chimney-ai repositories 'web' and 'bot' for pull requests "
        "related to the Search Pane feature. Identify any recent or critical pull "
        "requests that impact the Search Pane."
    )
    assert _normalize_pr_query(blob) == "search pane"


def test_path_is_false_positive_for_v3_search_api():
    assert _path_is_false_positive(
        "src/pages/api/v3/search/index.ts",
        ["search pane"],
    )
    assert not _path_is_false_positive(
        "src/components/agent/search-pane-sync.tsx",
        ["search pane"],
    )


def test_score_pr_demotes_v3_search_file_for_search_pane():
    score, reasons = _score_pr_relevance(
        {"title": "Improve search results", "body": ""},
        ["search", "pane"],
        ["src/pages/api/v3/search/index.ts"],
        phrases=["search pane"],
    )
    assert score == 0
    assert not any(r.startswith("file:") for r in reasons)


def test_score_pr_relevance_title_and_files():
    pr = {"title": "Add search pane filters", "body": ""}
    score, reasons = _score_pr_relevance(pr, ["search", "pane"], phrases=["search pane"])
    assert score >= 24
    assert any("title" in r for r in reasons)

    score2, reasons2 = _score_pr_relevance(
        {"title": "misc fix", "body": ""},
        ["search"],
        ["src/components/SearchPane.tsx", "src/utils/unrelated.py"],
    )
    assert score2 >= 9
    assert any("file:" in r for r in reasons2)


def test_score_pr_demotes_weak_partial_keyword_match():
    score, reasons = _score_pr_relevance(
        {"title": "Improve search results", "body": ""},
        ["search", "pane"],
        phrases=["search pane"],
    )
    assert "weak:partial-keyword" in reasons
    assert score < 15


def test_pr_verdict_skip_for_weak_match():
    from qa_agent.tools.github import _pr_verdict

    assert "SKIP" in _pr_verdict(12, ["weak:partial-keyword", "file:x"])
    assert "DEEP READ" in _pr_verdict(30, ["title-phrase:search pane"])


def test_discover_finds_pr_by_file_paths_when_title_unrelated():
    settings = MagicMock()
    settings.github_token = "ghp_test"
    settings.github_base_url = "https://api.github.com"
    settings.github_repos_list = ["chimney-ai/web"]
    settings.github_default_branch = "main"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache-discover-files")

    client = GitHubClient(settings)

    pr = {
        "number": 453,
        "title": "Agent chat filter improvements",
        "state": "closed",
        "body": "sync filters with profile",
        "user": {"login": "dev"},
        "head": {"ref": "feat/agent-filters"},
        "base": {"ref": "main", "repo": {"full_name": "chimney-ai/web"}},
        "html_url": "https://github.com/chimney-ai/web/pull/453",
        "updated_at": "2026-07-10T00:00:00Z",
    }

    with patch.object(client, "list_repo_pull_requests", return_value=[pr]), patch.object(
        client, "search_pull_requests", return_value=[]
    ), patch.object(
        client,
        "get_pr_files",
        return_value=[
            {
                "filename": "docs/PRD/agent/bff/search-pane-sync.md",
                "status": "modified",
                "additions": 40,
                "deletions": 2,
            },
            {
                "filename": "src/lib/bot/search-pane-filters.ts",
                "status": "modified",
                "additions": 10,
                "deletions": 1,
            },
        ],
    ):
        report = discover_relevant_pull_requests(client, "search pane", scan_limit=10, top_n=3)
        assert "chimney-ai/web #453" in report
        assert "search-pane-sync" in report
        assert "DEEP READ" in report


def test_discover_relevant_pull_requests():
    settings = MagicMock()
    settings.github_token = "ghp_test"
    settings.github_base_url = "https://api.github.com"
    settings.github_repos_list = ["chimney-ai/web"]
    settings.github_default_branch = "main"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache-discover")

    client = GitHubClient(settings)

    pr = {
        "number": 99,
        "title": "Search pane sync",
        "state": "open",
        "body": "filters",
        "user": {"login": "dev"},
        "head": {"ref": "feat"},
        "base": {"ref": "main", "repo": {"full_name": "chimney-ai/web"}},
        "html_url": "https://github.com/chimney-ai/web/pull/99",
    }

    with patch.object(client, "list_repo_pull_requests", return_value=[pr]), patch.object(
        client, "search_pull_requests", return_value=[]
    ), patch.object(
        client,
        "get_pr_files",
        return_value=[
            {"filename": "src/search/SearchPane.tsx", "status": "modified", "additions": 10, "deletions": 2}
        ],
    ):
        report = discover_relevant_pull_requests(client, "search pane tests", scan_limit=10, top_n=3)
        assert "PR Discovery" in report
        assert "chimney-ai/web #99" in report
        assert "SearchPane" in report
        assert "DEEP READ" in report


def test_github_search_pull_requests():
    settings = MagicMock()
    settings.github_token = "ghp_test"
    settings.github_base_url = "https://api.github.com"
    settings.github_repos_list = ["chimney-ai/web", "chimney-ai/bot"]
    settings.github_default_branch = "main"
    settings.cache_dir = Path("/tmp/qa-agent-test-cache")

    client = GitHubClient(settings)
    with patch("httpx.Client") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "items": [{"number": 1, "title": "Test PR", "state": "open", "user": {"login": "u"}}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        results = client.search_pull_requests("payment", limit=5)
        assert len(results) == 1


def test_refresh_existing_clone_falls_back_to_reset(tmp_path, monkeypatch):
    target = tmp_path / "web"
    target.mkdir()
    (target / ".git").mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        cwd = kwargs.get("cwd") or (cmd[2] if len(cmd) > 2 and cmd[0] == "git" else "")
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        if cmd[:4] == ["git", "-C", str(target), "pull"]:
            result.returncode = 1
            result.stderr = "fatal: Not possible to fast-forward"
        elif cmd[:4] == ["git", "-C", str(target), "fetch"]:
            result.returncode = 0
        elif cmd[:4] == ["git", "-C", str(target), "rev-parse"]:
            result.returncode = 0
            result.stdout = "main\n"
        elif cmd[:4] == ["git", "-C", str(target), "reset"]:
            result.returncode = 0
        else:
            result.returncode = 0
        return result

    monkeypatch.setattr("qa_agent.tools.github.subprocess.run", fake_run)
    action = _refresh_existing_clone(target)
    assert action == "fetch+reset"
    assert any("reset" in " ".join(c) for c in calls)
