"""Azure DevOps research source — Repos + Pull Requests, Wiki and Boards.

A single connector that lets the QA agent gather evidence from three Azure
DevOps surfaces:

* **Repos / Pull Requests** — discover PRs, read their changed files and file
  content (wire-level code evidence, like the GitHub connector).
* **Wiki** — search and read wiki pages (documentation / acceptance criteria).
* **Boards** — search and read work items (requirements / acceptance criteria).

Auth is a Personal Access Token (PAT) used as the password of HTTP Basic with
an empty username. When Azure DevOps is not configured the tools return a clear
"not configured" message instead of failing the run.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker, summarize_diff
from qa_agent.models.schemas import FileCache

API_VERSION = "7.0"  # default fallback; overridden by settings.azure_devops_api_version


def _azure_git_basic_header(pat: str) -> str:
    """HTTP Basic header for git (empty username + PAT), matching REST auth."""
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    return f"AUTHORIZATION: Basic {token}"


def _git_azure(
    args: list[str],
    *,
    pat: str,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run git with Azure DevOps Server PAT via http.extraHeader (not URL-embedded)."""
    cmd = ["git"]
    if pat:
        cmd += ["-c", f"http.extraHeader={_azure_git_basic_header(pat)}"]
    cmd += args
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )

_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "test", "case", "cases",
    "feature", "flow", "user", "make", "create", "verify", "should", "when", "then",
    "scenarios", "scenario", "acceptance", "criteria", "changes", "pull", "request",
    "برام", "بساز", "تست", "کیس", "راجب", "مورد", "برای", "یک", "که", "باید",
}


def _keywords(query: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
    keywords = [t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS]
    return list(dict.fromkeys(keywords))


class AzureDevOpsClient:
    """Minimal Azure DevOps REST client (Git, Wiki, Work Items)."""

    def __init__(self, settings: Settings | None = None, cost: CostTracker | None = None) -> None:
        self.settings = settings or get_settings()
        self.cost = cost
        self.cache = FileCache(self.settings.cache_dir)

    @property
    def configured(self) -> bool:
        return self.settings.azure_devops_configured

    @property
    def repos(self) -> list[str]:
        return self.settings.azure_devops_repos_list

    @property
    def primary_repo(self) -> str:
        repos = self.repos
        return repos[0] if repos else ""

    def _auth(self) -> tuple[str, str]:
        return ("", self.settings.azure_devops_pat)

    @property
    def _api_version(self) -> str:
        return (self.settings.azure_devops_api_version or API_VERSION).strip()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.cost:
            self.cost.record_azure_call()
        base = self.settings.azure_project_url()
        url = f"{base}/{path.lstrip('/')}"
        query = {"api-version": self._api_version, **(params or {})}
        with httpx.Client(timeout=60.0, verify=self.settings.httpx_verify) as client:
            response = client.get(url, params=query, auth=self._auth(), headers={"Accept": "application/json"})
            response.raise_for_status()
            return response.json()

    def _post(self, path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        if self.cost:
            self.cost.record_azure_call()
        base = self.settings.azure_project_url()
        url = f"{base}/{path.lstrip('/')}"
        query = {"api-version": self._api_version, **(params or {})}
        with httpx.Client(timeout=60.0, verify=self.settings.httpx_verify) as client:
            response = client.post(url, params=query, json=payload, auth=self._auth())
            response.raise_for_status()
            return response.json()

    # ── Repos / Pull Requests ──────────────────────────────────────────

    def list_pull_requests(self, repo: str, status: str = "all", limit: int = 30) -> list[dict]:
        cache_key = f"pr_list_{repo}_{status}_{limit}"
        cached = self.cache.get("azure", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        result = self._get(
            f"_apis/git/repositories/{repo}/pullrequests",
            {"searchCriteria.status": status, "$top": min(limit, 100)},
        )
        prs = result.get("value", [])
        self.cache.set("azure", cache_key, prs)
        return prs

    def get_pull_request(self, repo: str, pr_id: int) -> dict:
        cache_key = f"pr_{repo}_{pr_id}"
        cached = self.cache.get("azure", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        result = self._get(f"_apis/git/repositories/{repo}/pullrequests/{pr_id}")
        self.cache.set("azure", cache_key, result)
        return result

    def get_pr_changes(self, repo: str, pr_id: int) -> list[dict]:
        """Changed items in the last iteration of a PR (path + change type)."""
        cache_key = f"pr_changes_{repo}_{pr_id}"
        cached = self.cache.get("azure", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        iterations = self._get(
            f"_apis/git/repositories/{repo}/pullrequests/{pr_id}/iterations"
        ).get("value", [])
        if not iterations:
            return []
        last = iterations[-1].get("id")
        changes = self._get(
            f"_apis/git/repositories/{repo}/pullrequests/{pr_id}/iterations/{last}/changes"
        ).get("changeEntries", [])
        self.cache.set("azure", cache_key, changes)
        return changes

    def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        branch = ref or self.settings.azure_devops_default_branch
        cache_key = f"file_{repo}_{branch}_{path}"
        cached = self.cache.get("azure", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        result = self._get(
            f"_apis/git/repositories/{repo}/items",
            {
                "path": path,
                "includeContent": "true",
                "versionDescriptor.version": branch,
                "versionDescriptor.versionType": "branch",
            },
        )
        text = result.get("content", "") if isinstance(result, dict) else ""
        self.cache.set("azure", cache_key, text)
        return text

    # ── Wiki ────────────────────────────────────────────────────────────

    def wiki_pages(self, wiki: str) -> list[dict]:
        cache_key = f"wiki_pages_{wiki}"
        cached = self.cache.get("azure", cache_key, ttl_seconds=86400)
        if cached is not None:
            return cached
        result = self._get(
            f"_apis/wiki/wikis/{wiki}/pages",
            {"path": "/", "recursionLevel": "full"},
        )
        pages: list[dict] = []

        def _walk(node: dict) -> None:
            if node.get("path"):
                pages.append({"id": node.get("id"), "path": node.get("path")})
            for child in node.get("subPages") or []:
                _walk(child)

        _walk(result)
        self.cache.set("azure", cache_key, pages)
        return pages

    def wiki_page_text(self, wiki: str, path: str) -> str:
        result = self._get(
            f"_apis/wiki/wikis/{wiki}/pages",
            {"path": path, "includeContent": "true"},
        )
        return str(result.get("content", "")) if isinstance(result, dict) else ""

    # ── Boards / Work Items ──────────────────────────────────────────────

    def search_work_items(self, text: str, limit: int = 20) -> list[int]:
        safe = text.replace("'", " ").strip()
        wiql = (
            "SELECT [System.Id] FROM workitems "
            "WHERE [System.TeamProject] = @project "
            f"AND ([System.Title] CONTAINS '{safe}' OR [System.Description] CONTAINS '{safe}') "
            "ORDER BY [System.ChangedDate] DESC"
        )
        result = self._post("_apis/wit/wiql", {"query": wiql}, {"$top": limit})
        return [item.get("id") for item in result.get("workItems", []) if item.get("id")][:limit]

    def get_work_items(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        result = self._get(
            "_apis/wit/workitems",
            {"ids": ",".join(str(i) for i in ids), "$expand": "fields"},
        )
        return result.get("value", [])

    # ── Clone (for indexing) ─────────────────────────────────────────────

    def list_repository_names(self) -> list[str]:
        """All Git repository names in the configured project."""
        data = self._get("_apis/git/repositories")
        names = [str(r.get("name", "")).strip() for r in (data.get("value") or [])]
        return [n for n in names if n]

    def clone_configured_repo(self, repo: str, base_dir: Path) -> tuple[Path, str]:
        target = base_dir / repo
        pat = self.settings.azure_devops_pat

        if (target / ".git").exists():
            return target, self._refresh_clone(target, pat)

        # Leftover empty/partial dirs from a failed clone block `git clone`.
        if target.exists():
            shutil.rmtree(target)

        clone_url = self.settings.azure_repo_clone_url_for(repo)
        # Auth via http.extraHeader — URL-embedded credentials fail on many
        # on-prem Azure DevOps Server installs even when REST Basic works.
        clone = _git_azure(
            ["clone", "--depth", "1", clone_url, str(target)],
            pat=pat,
        )
        if clone.returncode != 0:
            detail = (clone.stderr or clone.stdout or "").strip()
            if pat:
                detail = detail.replace(pat, "***")
            raise RuntimeError(detail[:500] or "git clone failed")
        return target, "clone"

    def _refresh_clone(self, target: Path, pat: str) -> str:
        """Update an existing Azure clone with the same PAT header as clone."""
        pull = _git_azure(["pull", "--ff-only"], pat=pat, cwd=target)
        if pull.returncode == 0:
            if "Already up to date" in (pull.stdout or ""):
                return "up to date"
            return "pull"

        fetch = _git_azure(["fetch", "origin"], pat=pat, cwd=target)
        if fetch.returncode != 0:
            detail = (fetch.stderr or pull.stderr or pull.stdout or "").strip()
            if pat:
                detail = detail.replace(pat, "***")
            raise RuntimeError(detail[:500] or "git fetch failed")

        branch = _git_azure(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            pat=pat,
            cwd=target,
            check=True,
        ).stdout.strip()
        last_error = ""
        for reset_ref in (f"origin/{branch}", "origin/HEAD"):
            reset = _git_azure(["reset", "--hard", reset_ref], pat=pat, cwd=target)
            if reset.returncode == 0:
                return "fetch+reset"
            last_error = (reset.stderr or "").strip()

        detail = last_error or (pull.stderr or pull.stdout or "").strip()
        if pat:
            detail = detail.replace(pat, "***")
        raise RuntimeError(detail[:500] or "git reset failed")


def _score_pr(pr: dict, keywords: list[str]) -> int:
    title = (pr.get("title") or "").lower()
    desc = (pr.get("description") or "").lower()
    score = 0
    for kw in keywords:
        if kw in title:
            score += 12
        elif kw in desc:
            score += 4
    return score


def format_pr_summary(pr: dict, repo: str = "") -> str:
    pr_id = pr.get("pullRequestId", "?")
    author = (pr.get("createdBy") or {}).get("displayName", "")
    source = (pr.get("sourceRefName") or "").replace("refs/heads/", "")
    target = (pr.get("targetRefName") or "").replace("refs/heads/", "")
    repo_line = f"  repo={repo}\n" if repo else ""
    return (
        f"PR !{pr_id}: {pr.get('title', '')}\n"
        f"{repo_line}"
        f"  status={pr.get('status', '')} author={author}\n"
        f"  branch={source} → {target}\n"
        f"  description={str(pr.get('description', ''))[:500]}"
    )


def create_azure_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
) -> list:
    client = AzureDevOpsClient(settings, cost)
    cfg = settings or get_settings()
    repos_hint = ", ".join(cfg.azure_devops_repos_list) or "repo-name"
    max_diff = cfg.qa_agent_max_diff_lines

    def _not_configured() -> str:
        return (
            "Azure DevOps is not configured (needs AZURE_DEVOPS_PAT, "
            "AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT)."
        )

    @tool
    def azure_discover_relevant_prs(query: str, max_results: int = 8) -> str:
        """Scan recent Azure DevOps pull requests across configured repos and rank by query keywords.

        Pass a short feature name (e.g. 'search pane'). Returns PR IDs, titles and
        the repo to deep-read via azure_get_pr_changes.
        """
        if not client.configured:
            return _not_configured()
        if not client.repos:
            return "No Azure DevOps repositories configured (AZURE_DEVOPS_REPOS)."
        keywords = _keywords(query) or [query.strip()]
        scored: list[tuple[int, str, dict]] = []
        for repo in client.repos:
            try:
                prs = client.list_pull_requests(repo, status="all", limit=40)
            except httpx.HTTPStatusError:
                continue
            for pr in prs:
                score = _score_pr(pr, keywords)
                if score > 0:
                    scored.append((score, repo, pr))
        if not scored:
            return f"No relevant Azure DevOps PRs found for: {query}"
        scored.sort(key=lambda x: x[0], reverse=True)
        lines = [f"# Azure DevOps PR discovery — «{query}»", ""]
        for score, repo, pr in scored[:max_results]:
            lines.append(f"## score={score} | {repo} !{pr.get('pullRequestId')} | {pr.get('status')}")
            lines.append(f"Title: {pr.get('title', '')}")
            lines.append(
                f"QA action: azure_get_pr_changes(repo=\"{repo}\", pr_id={pr.get('pullRequestId')})"
            )
            lines.append("")
        return "\n".join(lines)

    @tool
    def azure_get_pull_request(pr_id: int, repo: str = "") -> str:
        """Get Azure DevOps pull request details. repo required when multiple repos configured."""
        if not client.configured:
            return _not_configured()
        target = repo or client.primary_repo
        if not target:
            return f"repo is required. Configured repos: {repos_hint}"
        pr = client.get_pull_request(target, pr_id)
        if cost:
            cost.add_source(f"azure-pr:{target}!{pr_id}")
        return format_pr_summary(pr, target)

    @tool
    def azure_get_pr_changes(pr_id: int, repo: str = "") -> str:
        """List the changed files of an Azure DevOps PR (path + change type). repo required when multiple repos configured."""
        if not client.configured:
            return _not_configured()
        target = repo or client.primary_repo
        if not target:
            return f"repo is required. Configured repos: {repos_hint}"
        changes = client.get_pr_changes(target, pr_id)
        if cost:
            cost.add_source(f"azure-pr:{target}!{pr_id}")
        if not changes:
            return f"PR !{pr_id} ({target}) has no listed changes."
        lines = [f"PR !{pr_id} changes ({target}):"]
        for entry in changes:
            item = entry.get("item") or {}
            lines.append(f"  {item.get('path', '')} [{entry.get('changeType', '')}]")
        return summarize_diff("\n".join(lines), max_lines=max_diff)

    @tool
    def azure_get_file_content(path: str, repo: str = "", ref: str = "") -> str:
        """Read a file from an Azure DevOps repo at a branch. repo required when multiple repos configured."""
        if not client.configured:
            return _not_configured()
        target = repo or client.primary_repo
        if not target:
            return f"repo is required. Configured repos: {repos_hint}"
        content = client.get_file_content(target, path, ref or None)
        max_chars = 6000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return content or f"File '{path}' is empty or not found on {target}."

    @tool
    def azure_search_work_items(query: str, max_results: int = 10) -> str:
        """Search Azure Boards work items (title/description) for requirements and acceptance criteria."""
        if not client.configured:
            return _not_configured()
        keywords = _keywords(query)
        term = keywords[0] if keywords else query.strip()
        try:
            ids = client.search_work_items(term, limit=max_results)
        except httpx.HTTPError as exc:
            return f"Work item search failed: {type(exc).__name__}"
        if not ids:
            return f"No work items found for: {query}"
        items = client.get_work_items(ids)
        lines = [f"Found {len(items)} work items for «{query}»:"]
        for item in items:
            fields = item.get("fields", {})
            lines.append(
                f"- #{item.get('id')} [{fields.get('System.WorkItemType', '')}] "
                f"{fields.get('System.Title', '')} (state={fields.get('System.State', '')})"
            )
        lines.append("\nRead full detail with azure_get_work_item(id).")
        return "\n".join(lines)

    @tool
    def azure_get_work_item(work_item_id: int) -> str:
        """Read a single Azure Boards work item's title, state and description/acceptance criteria."""
        if not client.configured:
            return _not_configured()
        items = client.get_work_items([work_item_id])
        if not items:
            return f"Work item #{work_item_id} not found."
        fields = items[0].get("fields", {})
        if cost:
            cost.add_source(f"azure-workitem:{work_item_id}")
        desc = _strip_html(str(fields.get("System.Description", "")))
        acc = _strip_html(str(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")))
        lines = [
            f"#{work_item_id} [{fields.get('System.WorkItemType', '')}] {fields.get('System.Title', '')}",
            f"State: {fields.get('System.State', '')}",
        ]
        if desc:
            lines.append(f"\nDescription:\n{desc[:3000]}")
        if acc:
            lines.append(f"\nAcceptance criteria:\n{acc[:3000]}")
        return "\n".join(lines)

    @tool
    def azure_wiki_search(query: str, max_results: int = 8) -> str:
        """Search the configured Azure DevOps wiki by keyword (page path match)."""
        if not client.configured:
            return _not_configured()
        wiki = cfg.azure_devops_wiki
        if not wiki:
            return "No Azure DevOps wiki configured (AZURE_DEVOPS_WIKI)."
        try:
            pages = client.wiki_pages(wiki)
        except httpx.HTTPError as exc:
            return f"Wiki listing failed: {type(exc).__name__}"
        keywords = _keywords(query)
        matched = [
            p for p in pages
            if any(kw in str(p.get("path", "")).lower() for kw in keywords)
        ] or pages
        lines = [f"Wiki pages for «{query}»:"]
        for page in matched[:max_results]:
            lines.append(f"- {page.get('path')}")
        lines.append("\nRead a page with azure_wiki_get_page(path).")
        return "\n".join(lines)

    @tool
    def azure_wiki_get_page(path: str) -> str:
        """Read an Azure DevOps wiki page's markdown content by its path."""
        if not client.configured:
            return _not_configured()
        wiki = cfg.azure_devops_wiki
        if not wiki:
            return "No Azure DevOps wiki configured (AZURE_DEVOPS_WIKI)."
        try:
            text = client.wiki_page_text(wiki, path)
        except httpx.HTTPError as exc:
            return f"Could not read wiki page '{path}': {type(exc).__name__}"
        if cost:
            cost.add_source(f"azure-wiki:{path}")
        max_chars = 6000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return text or f"Wiki page '{path}' is empty."

    return [
        azure_discover_relevant_prs,
        azure_get_pull_request,
        azure_get_pr_changes,
        azure_get_file_content,
        azure_search_work_items,
        azure_get_work_item,
        azure_wiki_search,
        azure_wiki_get_page,
    ]


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html

    return _html.unescape(text).strip()
