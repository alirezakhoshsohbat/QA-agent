from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker, summarize_diff
from qa_agent.domain import load_domain_rules
from qa_agent.models.schemas import FileCache

_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "test", "case", "cases",
    "feature", "flow", "user", "make", "create", "verify", "should", "when", "then",
    "analyze", "analyse", "repositories", "repository", "pull", "requests", "request",
    "related", "identify", "recent", "critical", "impact", "especially", "undocumented",
    "behaviors", "behaviour", "changes", "extract", "wire", "level", "details", "detail",
    "error", "scenarios", "scenario", "additional", "acceptance", "criteria", "complement",
    "outline", "researcher", "findings", "application", "produce", "important", "across",
    "configured", "repos", "implement", "implementing", "return", "their", "level",
    "response", "responses", "behaviors", "behavior", "using", "triage", "discover",
    "deep", "read", "summary", "diff", "files", "file", "list", "ranked", "rank",
    "برام", "بساز", "تست", "کیس", "راجب", "مورد", "برای", "یک", "که", "باید",
}

def _repo_slug(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _git_run(args: list[str], *, cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _refresh_existing_clone(target_dir: Path) -> str:
    """Update an existing clone; fall back to fetch+reset when ff-only pull fails."""
    pull = _git_run(["pull", "--ff-only"], cwd=target_dir)
    if pull.returncode == 0:
        if "Already up to date" in (pull.stdout or ""):
            return "up to date"
        return "pull"

    fetch = _git_run(["fetch", "origin"], cwd=target_dir)
    if fetch.returncode != 0:
        detail = (fetch.stderr or pull.stderr or pull.stdout or "").strip()
        raise RuntimeError(detail[:500] or "git fetch failed")

    branch = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target_dir, check=True).stdout.strip()
    last_error = ""
    for reset_ref in (f"origin/{branch}", "origin/HEAD"):
        reset = _git_run(["reset", "--hard", reset_ref], cwd=target_dir)
        if reset.returncode == 0:
            return "fetch+reset"
        last_error = (reset.stderr or "").strip()

    detail = last_error or (pull.stderr or pull.stdout or "").strip()
    raise RuntimeError(detail[:500] or "git reset failed")


def _query_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
    keywords: list[str] = []
    for token in tokens:
        if len(token) >= 3 and token not in _STOP_WORDS:
            keywords.append(token)
    return list(dict.fromkeys(keywords))


def _normalize_pr_query(query: str) -> str:
    """Reduce long orchestrator delegation text to a short feature-focused query."""
    stripped = query.strip()
    if not stripped:
        return stripped

    lowered = stripped.lower()
    for pattern in load_domain_rules().pr_query_focus_patterns:
        match = pattern.search(lowered)
        if match:
            return match.group(0)

    for match in re.finditer(r"['\"]([^'\"]{3,48})['\"]", stripped):
        inner = match.group(1).strip()
        lowered_inner = inner.lower()
        # Skip repo slugs (owner/repo) and generic "repository" mentions — they
        # are not the feature name.
        if "/" in inner or "repositor" in lowered_inner:
            continue
        if re.fullmatch(r"[a-z0-9_-]+", lowered_inner) and len(inner.split()) == 1:
            continue
        if len(_query_keywords(inner)) >= 1:
            return inner

    keywords = _query_keywords(stripped)
    if len(keywords) >= 2:
        return " ".join(keywords[:3])
    if keywords:
        return keywords[0]
    return stripped[:80]


def _feature_path_markers(focused_query: str) -> list[str]:
    normalized = focused_query.lower().strip()
    markers: list[str] = []
    for key, values in load_domain_rules().pr_path_markers.items():
        if key in normalized:
            markers.extend(values)
            break
    if not markers:
        compact = normalized.replace(" ", "-")
        if compact:
            markers.append(compact)
    deduped: list[str] = []
    seen: set[str] = set()
    for marker in markers:
        key = marker.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(marker)
    return deduped


def _research_pr_search_terms(query: str) -> tuple[str, list[str], list[str], list[str]]:
    """Return focused query, GitHub search terms, keywords, and phrases."""
    focused = _normalize_pr_query(query)
    keywords = _query_keywords(focused)[:6]
    phrases = _query_phrases(focused, keywords)
    if not keywords:
        keywords = [w for w in focused.split() if len(w) >= 3][:3] or [focused[:20]]

    terms: list[str] = []
    normalized = focused.lower()
    for key, aliases in load_domain_rules().pr_search_aliases.items():
        if key in normalized:
            terms.extend(aliases)
            break

    terms.extend(phrases)
    terms.extend(keywords)
    if focused:
        terms.append(focused)

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term.strip())
    return focused, deduped[:10], keywords, phrases


def _path_matches_feature_markers(path: str, markers: list[str]) -> bool:
    path_lower = path.lower()
    return any(marker.lower() in path_lower for marker in markers)


def _score_pr_file_markers(
    file_paths: list[str],
    markers: list[str],
    phrases: list[str],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    seen: set[str] = set()
    for path in file_paths:
        if _path_is_false_positive(path, phrases):
            continue
        if not _path_matches_feature_markers(path, markers):
            continue
        if path in seen:
            continue
        seen.add(path)
        score += 22
        reasons.append(f"file-marker:{path}")
    return score, reasons


def _path_is_false_positive(path: str, phrases: list[str]) -> bool:
    """Drop file paths that match a generic token but not the actual feature.

    Driven entirely by the active domain profile's ``pr_false_positives`` rules
    (e.g. for a "search pane" query, demote generic ``/api/v3/search/`` paths
    that are unrelated to the search-pane feature).
    """
    path_lower = path.lower()
    compact_path = path_lower.replace("-", "").replace("_", "")
    joined_phrases = " ".join(p.lower() for p in phrases)

    for rule in load_domain_rules().pr_false_positives:
        if not all(term in joined_phrases for term in rule.when_all):
            continue
        if any(
            keep in path_lower or keep.replace("-", "").replace("_", "") in compact_path
            for keep in rule.keep_if_contains
        ):
            continue
        if any(marker in path_lower for marker in rule.drop_markers):
            return True
    return False


def _query_phrases(query: str, keywords: list[str]) -> list[str]:
    """Multi-word phrases from the query (e.g. 'search pane')."""
    lowered = query.lower()
    phrases: list[str] = []
    for match in re.finditer(r"[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)+", lowered):
        phrase = match.group(0).strip()
        parts = phrase.split()
        if len(parts) >= 2 and not all(p in _STOP_WORDS for p in parts):
            phrases.append(phrase)
    if len(keywords) >= 2:
        phrases.append(" ".join(keywords[:2]))
    return list(dict.fromkeys(phrases))


def _path_matches_feature(path: str, keywords: list[str], phrases: list[str]) -> bool:
    path_lower = path.lower()
    for phrase in phrases:
        compact = phrase.replace(" ", "")
        if phrase.replace(" ", "-") in path_lower or phrase.replace(" ", "_") in path_lower:
            return True
        if compact in path_lower.replace("-", "").replace("_", ""):
            return True
    matched = sum(1 for kw in keywords if kw in path_lower)
    return matched >= 2 or (len(keywords) == 1 and matched == 1)


def _score_pr_relevance(
    pr: dict,
    keywords: list[str],
    file_paths: list[str] | None = None,
    phrases: list[str] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    title = (pr.get("title") or "").lower()
    body = (pr.get("body") or "").lower()
    phrases = phrases or []

    for phrase in phrases:
        if phrase in title:
            score += 30
            reasons.append(f"title-phrase:{phrase}")
        elif phrase in body:
            score += 12
            reasons.append(f"body-phrase:{phrase}")

    for kw in keywords:
        if kw in title:
            score += 12
            reasons.append(f"title:{kw}")
        elif kw in body:
            score += 4
            reasons.append(f"body:{kw}")

    seen_files: set[str] = set()
    for path in file_paths or []:
        if _path_is_false_positive(path, phrases):
            continue
        if not _path_matches_feature(path, keywords, phrases):
            continue
        path_lower = path.lower()
        for kw in keywords:
            if kw in path_lower and path not in seen_files:
                score += 9
                reasons.append(f"file:{path}")
                seen_files.add(path)
                break

    # Demote generic single-keyword file hits (e.g. "search" in search-v3 unrelated to "search pane")
    if len(keywords) >= 2 and score > 0:
        has_strong = any(
            r.startswith("title-phrase:") or r.startswith("body-phrase:") or "pane" in r
            for r in reasons
        )
        kw_hits = sum(1 for kw in keywords if any(kw in r for r in reasons))
        if not has_strong and kw_hits < 2:
            score = max(0, score - 15)
            reasons.append("weak:partial-keyword")

    return score, reasons


def _pr_verdict(score: int, reasons: list[str]) -> str:
    if any(r.startswith("file-marker:") for r in reasons) and score >= 20:
        return "DEEP READ — fetch diff (max 1 per feature)"
    if score >= 25 and "weak:partial-keyword" not in reasons:
        return "DEEP READ — fetch diff (max 1 per feature)"
    if score >= 15 and "weak:partial-keyword" not in reasons:
        return "SKIM — file list only"
    return "SKIP — weak or unrelated match"


def _pr_key(repo: str, number: int | str) -> str:
    return f"{repo}#{number}"


def discover_relevant_pull_requests(
    client: GitHubClient,
    query: str,
    *,
    scan_limit: int = 40,
    top_n: int = 8,
    deep_file_scan: int = 10,
) -> str:
    """Scan recent PRs across repos, score by query keywords, rank with file paths."""
    focused_query, search_terms, keywords, phrases = _research_pr_search_terms(query)
    path_markers = _feature_path_markers(focused_query)

    candidates: dict[str, dict[str, Any]] = {}
    per_repo = max(10, scan_limit // max(len(client.repos), 1))

    for repo in client.repos:
        for state in ("open", "closed"):
            try:
                prs = client.list_repo_pull_requests(repo, state=state, limit=per_repo // 2)
            except httpx.HTTPStatusError:
                continue
            for pr in prs:
                key = _pr_key(repo, pr.get("number", 0))
                candidates[key] = {"pr": pr, "repo": repo, "score": 0, "reasons": [], "files": []}

    for term in search_terms:
        try:
            for pr in client.search_pull_requests(term, limit=12, state="all"):
                repo = _repo_from_pr(pr)
                if repo and repo not in client.repos:
                    continue
                if not repo and client.repos:
                    repo = client.repos[0]
                number = pr.get("number", 0)
                key = _pr_key(repo or "?", number)
                if key not in candidates:
                    candidates[key] = {"pr": pr, "repo": repo or "", "score": 0, "reasons": [], "files": []}
        except httpx.HTTPStatusError:
            continue

    for item in candidates.values():
        base_score, reasons = _score_pr_relevance(item["pr"], keywords, phrases=phrases)
        item["score"] = base_score
        item["reasons"] = reasons

    file_scan_budget = min(len(candidates), max(deep_file_scan + 12, scan_limit // 2))
    file_scan_targets: list[dict[str, Any]] = []

    ranked_for_files = sorted(
        candidates.values(),
        key=lambda item: (
            item["score"],
            str(item["pr"].get("updated_at", "")),
        ),
        reverse=True,
    )
    for item in ranked_for_files[:file_scan_budget]:
        if item.get("files"):
            continue
        file_scan_targets.append(item)

    for item in file_scan_targets:
        repo = item["repo"]
        number = item["pr"].get("number")
        if not repo or not number:
            continue
        try:
            files = client.get_pr_files(int(number), repo)
        except (httpx.HTTPStatusError, ValueError):
            continue
        paths = [f.get("filename", "") for f in files if f.get("filename")]
        item["files"] = paths
        item["file_count"] = len(paths)
        extra, file_reasons = _score_pr_relevance(item["pr"], keywords, paths, phrases=phrases)
        marker_score, marker_reasons = _score_pr_file_markers(paths, path_markers, phrases)
        item["score"] += extra + marker_score
        item["reasons"].extend(file_reasons)
        item["reasons"].extend(marker_reasons)

    scored = [item for item in candidates.values() if item["score"] > 0]
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]

    if not top:
        return (
            f"No relevant PRs found for query: {focused_query}\n"
            f"(normalized from: {query[:120]}{'…' if len(query) > 120 else ''})\n"
            f"Search terms tried: {', '.join(search_terms)}\n"
            f"Path markers: {', '.join(path_markers) or '—'}\n"
            f"Scanned {len(candidates)} PRs · fetched files for {len(file_scan_targets)} · 0 matched\n"
            "Doc-only coverage is likely sufficient unless a PR title hides the feature — "
            f"check merged PRs touching the feature's paths ({', '.join(path_markers) or focused_query}) manually."
        )

    lines = [
        f"# PR Discovery — «{focused_query}»",
        f"Search terms: {', '.join(search_terms)}",
        f"Keywords: {', '.join(keywords)}",
        f"Phrases: {', '.join(phrases) or '—'}",
        f"Scanned {len(candidates)} PRs · fetched files for {len(file_scan_targets)} · "
        f"{len(scored)} matched · showing top {len(top)}",
        "",
        "Deep-read **every DEEP READ and SKIM row** below (up to run limit) via "
        "github_get_pr_file_list then github_get_pr_changes. Titles alone are not enough.",
        "",
    ]

    for idx, item in enumerate(top, 1):
        pr = item["pr"]
        repo = item["repo"]
        number = pr.get("number", "?")
        state = pr.get("state", "")
        title = pr.get("title", "")
        score = item["score"]
        reasons = ", ".join(dict.fromkeys(item["reasons"][:8])) or "keyword overlap"
        files = item.get("files") or []
        file_preview = ", ".join(files[:6])
        if len(files) > 6:
            file_preview += f" (+{len(files) - 6} more)"

        rec = _pr_verdict(score, item["reasons"])
        lines.extend(
            [
                f"## {idx}. score={score} | {repo} #{number} | {state}",
                f"Title: {title}",
                f"Match: {reasons}",
                f"Files ({item.get('file_count', len(files))}): {file_preview or 'not fetched'}",
                f"QA action: {rec}",
                f"URL: {pr.get('html_url', '')}",
                "",
            ]
        )

    lines.extend(
        [
            "## Recommended QA workflow",
            "1. Read Outline PRD acceptance criteria first",
            "2. For each DEEP READ / SKIM row above: github_get_pr_file_list → github_get_pr_changes",
            "3. Add @pr-{number} scenarios for behavior found in diffs but not in docs",
        ]
    )
    return "\n".join(lines)


def format_pr_file_list(files: list[dict], pr_number: int, repo: str = "") -> str:
    header = f"PR #{pr_number} file list"
    if repo:
        header += f" ({repo})"
    lines = [header + ":"]
    for change in files:
        filename = change.get("filename", "")
        status = change.get("status", "")
        adds = change.get("additions", 0)
        dels = change.get("deletions", 0)
        lines.append(f"  {filename} [{status}] +{adds}/-{dels}")
    return "\n".join(lines)


def _repo_from_pr(pr: dict) -> str:
    base = pr.get("base", {}) or {}
    repo_obj = base.get("repo", {}) or {}
    if full := repo_obj.get("full_name"):
        return full
    url = pr.get("repository_url", "")
    if url and "repos/" in url:
        return url.split("repos/")[-1].split("/pulls")[0]
    return ""


class GitHubClient:
    """GitHub REST API client — supports multiple repositories."""

    def __init__(self, settings: Settings | None = None, cost: CostTracker | None = None) -> None:
        self.settings = settings or get_settings()
        self.cost = cost
        self.cache = FileCache(self.settings.cache_dir)
        self._headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def repos(self) -> list[str]:
        return self.settings.github_repos_list

    @property
    def primary_repo(self) -> str:
        repos = self.repos
        return repos[0] if repos else ""

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.cost:
            self.cost.record_github_call()
        url = f"{self.settings.github_base_url.rstrip('/')}/{path.lstrip('/')}"
        with httpx.Client(timeout=60.0) as client:
            response = client.get(url, headers=self._headers, params=params or {})
            response.raise_for_status()
            return response.json()

    def search_pull_requests(self, query: str, limit: int = 10, state: str = "all") -> list[dict]:
        repos = self.repos
        cache_key = f"pr_search_{query}_{state}_{limit}_{','.join(repos)}"
        cached = self.cache.get("github", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        items: list[dict] = []
        if repos:
            # Per-repo search avoids GitHub 422 on OR queries
            for repo in repos:
                q_parts = ["is:pr", query, f"repo:{repo}"]
                if state in {"open", "closed"}:
                    q_parts.append(f"is:{state}")
                try:
                    result = self._get(
                        "search/issues",
                        {"q": " ".join(q_parts), "per_page": min(limit, 30), "sort": "updated"},
                    )
                    items.extend(result.get("items", []))
                except httpx.HTTPStatusError:
                    continue
        else:
            q_parts = ["is:pr", query]
            if state in {"open", "closed"}:
                q_parts.append(f"is:{state}")
            result = self._get(
                "search/issues",
                {"q": " ".join(q_parts), "per_page": min(limit, 30), "sort": "updated"},
            )
            items = result.get("items", [])

        # Deduplicate by html_url
        seen: set[str] = set()
        deduped: list[dict] = []
        for item in items:
            key = item.get("html_url", str(item.get("id")))
            if key not in seen:
                seen.add(key)
                deduped.append(item)

        deduped.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        deduped = deduped[:limit]
        self.cache.set("github", cache_key, deduped)
        return deduped

    def list_repo_pull_requests(self, repo: str, state: str = "open", limit: int = 30) -> list[dict]:
        cache_key = f"pr_list_{repo}_{state}_{limit}"
        cached = self.cache.get("github", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        results = self._get(
            f"repos/{repo}/pulls",
            {
                "state": state,
                "per_page": min(limit, 100),
                "sort": "updated",
                "direction": "desc",
            },
        )
        self.cache.set("github", cache_key, results)
        return results

    def list_pull_requests(self, state: str = "open", limit: int = 10) -> list[dict]:
        if not self.repos:
            return []

        all_prs: list[dict] = []
        per_repo = max(5, limit // len(self.repos) + 1)
        for repo in self.repos:
            cache_key = f"pr_list_{repo}_{state}_{per_repo}"
            cached = self.cache.get("github", cache_key, ttl_seconds=3600)
            if cached is not None:
                all_prs.extend(cached)
                continue

            results = self._get(
                f"repos/{repo}/pulls",
                {"state": state, "per_page": per_repo, "sort": "updated", "direction": "desc"},
            )
            self.cache.set("github", cache_key, results)
            all_prs.extend(results)

        all_prs.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        return all_prs[:limit]

    def get_pull_request(self, number: int, repo: str | None = None) -> dict:
        repo = repo or self.primary_repo
        if not repo:
            raise ValueError("repo is required (e.g. owner/repo)")

        cache_key = f"pr_{repo}_{number}"
        cached = self.cache.get("github", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        result = self._get(f"repos/{repo}/pulls/{number}")
        self.cache.set("github", cache_key, result)
        return result

    def get_pr_files(self, number: int, repo: str | None = None) -> list[dict]:
        repo = repo or self.primary_repo
        if not repo:
            raise ValueError("repo is required (e.g. owner/repo)")

        cache_key = f"pr_files_{repo}_{number}"
        cached = self.cache.get("github", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        result = self._get(f"repos/{repo}/pulls/{number}/files", {"per_page": 100})
        self.cache.set("github", cache_key, result)
        return result

    def get_file_content(self, path: str, ref: str | None = None, repo: str | None = None) -> str:
        repo = repo or self.primary_repo
        if not repo:
            raise ValueError("repo is required (e.g. owner/repo)")

        ref = ref or self.settings.github_default_branch
        cache_key = f"file_{repo}_{ref}_{path}"
        cached = self.cache.get("github", cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        result = self._get(f"repos/{repo}/contents/{path}", {"ref": ref})
        encoding = result.get("encoding", "")
        content = result.get("content", "")
        if encoding == "base64":
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        else:
            text = content if isinstance(content, str) else str(result)
        self.cache.set("github", cache_key, text)
        return text

    def _clone_one(self, repo_url: str, target_dir: Path, repo: str = "") -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        if (target_dir / ".git").exists():
            _refresh_existing_clone(target_dir)
            return target_dir

        # Prefer gh CLI — uses existing login, faster for private org repos
        if repo and shutil.which("gh"):
            code = subprocess.run(
                ["gh", "repo", "clone", repo, str(target_dir), "--", "--depth", "1"],
                capture_output=True,
                text=True,
            ).returncode
            if code == 0:
                return target_dir

        token = self.settings.github_token
        if token and "github.com" in repo_url and "@" not in repo_url.split("://")[1]:
            auth_url = repo_url.replace("://", f"://x-access-token:{token}@", 1)
        else:
            auth_url = repo_url
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", auth_url, str(target_dir)],
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            detail = (clone.stderr or clone.stdout or "").strip()
            raise RuntimeError(detail[:500] or "git clone failed")
        return target_dir

    def clone_configured_repo(self, repo: str, base_dir: Path) -> tuple[Path, str]:
        """Clone or pull one configured repo; returns path and sync action label."""
        slug = _repo_slug(repo)
        target = base_dir / slug
        if (target / ".git").exists():
            action = _refresh_existing_clone(target)
            return target, action

        self._clone_one(self.settings.github_repo_url_for(repo), target, repo=repo)
        return target, "clone"

    def clone_repo(self, target_dir: Path, repo_url: str | None = None) -> Path:
        url = repo_url or self.settings.github_repo_url
        if not url:
            raise ValueError("GitHub repo URL is not configured")
        return self._clone_one(url, target_dir)

    def clone_all_repos(self, base_dir: Path) -> list[Path]:
        """Clone each configured repo into base_dir/{slug}/."""
        base_dir.mkdir(parents=True, exist_ok=True)
        cloned: list[Path] = []
        for repo in self.repos:
            path, _action = self.clone_configured_repo(repo, base_dir)
            cloned.append(path)
        return cloned


def format_pr_summary(pr: dict) -> str:
    number = pr.get("number") or pr.get("id", "?")
    user = pr.get("user", {}) or {}
    author = user.get("login", "")
    repo = _repo_from_pr(pr)
    if "head" in pr and "base" in pr:
        branch = f"{pr.get('head', {}).get('ref', '')} → {pr.get('base', {}).get('ref', '')}"
    else:
        branch = ""
    repo_line = f"  repo={repo}\n" if repo else ""
    return (
        f"PR #{number}: {pr.get('title', '')}\n"
        f"{repo_line}"
        f"  state={pr.get('state', '')} author={author}\n"
        f"  branch={branch}\n"
        f"  url={pr.get('html_url', '')}\n"
        f"  description={str(pr.get('body', ''))[:500]}"
    )


def format_pr_changes(files: list[dict], pr_number: int, repo: str = "", max_diff_lines: int = 500) -> str:
    header = f"PR #{pr_number} changes"
    if repo:
        header += f" ({repo})"
    lines = [f"{header}:"]
    for change in files:
        filename = change.get("filename", "")
        status = change.get("status", "")
        patch = change.get("patch", "") or ""
        patch = summarize_diff(patch, max_lines=max_diff_lines)
        lines.append(f"\n--- {filename} ({status}) ---\n{patch[:4000]}")
    return "\n".join(lines)


def create_github_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
    max_prs: int | None = None,
) -> list:
    client = GitHubClient(settings, cost)
    cfg = settings or get_settings()
    pr_limit = max_prs or cfg.qa_agent_max_prs
    scan_limit = cfg.qa_agent_pr_scan_limit
    deep_limit = cfg.qa_agent_max_prs_deep
    max_diff = cfg.qa_agent_max_diff_lines
    repos_hint = ", ".join(cfg.github_repos_list) or "owner/repo"

    @tool
    def github_discover_relevant_prs(query: str) -> str:
        """Scan recent PRs: multi-term GitHub search + file-path matching (finds PRs even when title omits the feature). Pass short feature name only (e.g. 'search pane')."""
        if not cfg.github_repos_list:
            return "GITHUB_REPOS is not configured (format: owner/repo,owner/repo)."
        return discover_relevant_pull_requests(
            client,
            query,
            scan_limit=scan_limit,
            top_n=max(pr_limit + 4, deep_limit + 5, 10),
            deep_file_scan=min(scan_limit, max(deep_limit * 6, pr_limit * 5, 20)),
        )

    @tool
    def github_list_pull_requests(state: str = "all", max_results: int = 30) -> str:
        """List recent pull requests across configured repos (metadata only). Use to browse when search misses."""
        if not cfg.github_repos_list:
            return "GITHUB_REPOS is not configured."
        all_prs: list[dict] = []
        per_repo = max(5, max_results // len(cfg.github_repos_list))
        states = ["open", "closed"] if state == "all" else [state]
        for repo in cfg.github_repos_list:
            for st in states:
                try:
                    all_prs.extend(client.list_repo_pull_requests(repo, state=st, limit=per_repo))
                except httpx.HTTPStatusError:
                    continue
        all_prs.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        if not all_prs:
            return "No pull requests found."
        return "\n\n".join(format_pr_summary(pr) for pr in all_prs[:max_results])

    @tool
    def github_get_pr_file_list(number: int, repo: str = "") -> str:
        """List changed files in a PR (paths + stats, no diff). Cheap way to assess blast radius before reading diffs."""
        target_repo = repo or client.primary_repo
        if not target_repo:
            return f"repo is required. Configured repos: {repos_hint}"
        files = client.get_pr_files(number, target_repo)
        if cost:
            cost.add_source(f"pr:{target_repo}#{number}")
        return format_pr_file_list(files, number, target_repo)

    @tool
    def github_search_pull_requests(query: str, max_results: int = 10) -> str:
        """Search GitHub pull requests by keyword across configured repos. Pass a short term; tries feature aliases when the query matches a known feature."""
        if not cfg.github_repos_list:
            return "GITHUB_REPOS is not configured (format: owner/repo,owner/repo)."

        _, search_terms, _, _ = _research_pr_search_terms(query)
        merged: list[dict] = []
        seen_urls: set[str] = set()
        for term in search_terms[:6]:
            for pr in client.search_pull_requests(term, limit=min(max_results, 15)):
                url = pr.get("html_url", str(pr.get("id")))
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(pr)

        if not merged:
            listed = client.list_pull_requests(state="all", limit=30)
            query_lower = _normalize_pr_query(query).lower()
            merged = [
                pr
                for pr in listed
                if query_lower in pr.get("title", "").lower()
                or query_lower in str(pr.get("body", "")).lower()
            ]

        if not merged:
            return "No pull requests found."
        merged.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
        return "\n\n".join(format_pr_summary(pr) for pr in merged[:max_results])

    @tool
    def github_get_pull_request(number: int, repo: str = "") -> str:
        """Get pull request details. repo is required when multiple repos are configured (e.g. owner/repo)."""
        target_repo = repo or client.primary_repo
        if not target_repo:
            return f"repo is required. Configured repos: {repos_hint}"
        pr = client.get_pull_request(number, target_repo)
        if cost:
            cost.add_source(f"pr:{target_repo}#{number}")
        return format_pr_summary(pr)

    @tool
    def github_get_pr_changes(number: int, repo: str = "") -> str:
        """Get summarized diff for a PR. repo required (e.g. owner/repo)."""
        target_repo = repo or client.primary_repo
        if not target_repo:
            return f"repo is required. Configured repos: {repos_hint}"
        files = client.get_pr_files(number, target_repo)
        if cost:
            cost.add_source(f"pr:{target_repo}#{number}")
        return format_pr_changes(files, number, target_repo, max_diff_lines=max_diff)

    @tool
    def github_get_file_content(path: str, repo: str = "", ref: str = "") -> str:
        """Read a file from a GitHub repo. repo required when multiple repos configured."""
        target_repo = repo or client.primary_repo
        if not target_repo:
            return f"repo is required. Configured repos: {repos_hint}"
        content = client.get_file_content(path, ref or None, target_repo)
        max_chars = 6000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return content

    return [
        github_discover_relevant_prs,
        github_list_pull_requests,
        github_get_pr_file_list,
        github_search_pull_requests,
        github_get_pull_request,
        github_get_pr_changes,
        github_get_file_content,
    ]
