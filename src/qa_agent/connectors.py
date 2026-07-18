"""Live connectivity checks for the configured research connectors.

Each ``test_connector`` call makes a single, cheap, read-only request against
the real service so the Web UI can tell the user — with certainty — whether a
connector is actually reachable with the current credentials, instead of only
reflecting whether values are filled in.

Tests run against the currently-saved settings, optionally overlaid with
unsaved form values sent from the Settings panel (so the user can type a new
key and verify it before persisting). Secrets left blank fall back to the saved
value; blank non-secret fields fall back too, so a partial edit never breaks a
test.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from qa_agent.config import Settings, get_settings
from qa_agent.preferences import (
    SECRET_FIELDS,
    is_secret_unchanged,
    sanitize_preferences,
)

CONNECTORS: tuple[str, ...] = ("outline", "confluence", "github", "azure", "openapi")


def settings_with_overrides(overrides: dict[str, Any] | None) -> Settings:
    """Base settings overlaid with non-empty, changed values from the form.

    Empty strings and unchanged secrets are dropped so the test always falls
    back to the persisted value rather than wiping it out mid-edit.
    """
    base = get_settings()
    if not overrides:
        return base
    cleaned = sanitize_preferences(overrides)
    for key in list(cleaned.keys()):
        value = cleaned[key]
        if key in SECRET_FIELDS:
            if is_secret_unchanged(value, str(getattr(base, key, "") or "")):
                cleaned.pop(key, None)
        elif isinstance(value, str) and not value:
            cleaned.pop(key, None)
    if not cleaned:
        return base
    return base.model_copy(update=cleaned)


def _result(ok: bool, message: str, detail: str = "", configured: bool = True) -> dict[str, Any]:
    return {"ok": ok, "configured": configured, "message": message, "detail": detail}


def _not_configured(message: str) -> dict[str, Any]:
    return _result(False, message, configured=False)


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    if status in (401, 403):
        return f"احراز هویت ناموفق بود (HTTP {status}) — کلید/توکن را بررسی کن"
    if status == 404:
        return f"آدرس یافت نشد (HTTP {status}) — Base URL یا مسیر را بررسی کن"
    return f"سرویس خطا برگرداند (HTTP {status})"


def _test_outline(s: Settings) -> dict[str, Any]:
    from qa_agent.tools.outline import OutlineClient

    if not (s.outline_api_key and s.outline_base_url):
        return _not_configured("Base URL یا API Key تنظیم نشده")
    try:
        data = OutlineClient(s)._post("auth.info", {})
    except httpx.HTTPStatusError as exc:
        return _result(False, _http_error_message(exc))
    except httpx.HTTPError as exc:
        return _result(False, "اتصال برقرار نشد — Base URL را بررسی کن", str(exc)[:200])

    info = data.get("data", {}) if isinstance(data, dict) else {}
    user = (info.get("user") or {}).get("name", "")
    team = (info.get("team") or {}).get("name", "")
    who = " · ".join(x for x in (user, team) if x)
    return _result(True, "اتصال برقرار شد", who)


def _test_confluence(s: Settings) -> dict[str, Any]:
    from qa_agent.tools.confluence import ConfluenceClient

    if not s.confluence_configured:
        return _not_configured("Base URL یا API Token تنظیم نشده")
    try:
        data = ConfluenceClient(s)._get("space", {"limit": 1})
    except httpx.HTTPStatusError as exc:
        return _result(False, _http_error_message(exc))
    except httpx.HTTPError as exc:
        return _result(False, "اتصال برقرار نشد — Base URL را بررسی کن", str(exc)[:200])

    size = data.get("size") if isinstance(data, dict) else None
    detail = f"{size} space در دسترس" if isinstance(size, int) else ""
    return _result(True, "اتصال برقرار شد", detail)


def _test_github(s: Settings) -> dict[str, Any]:
    token = (s.github_token or "").strip()
    repos = s.github_repos_list
    if not token and not repos:
        return _not_configured("Token یا Repos تنظیم نشده")

    base = s.github_base_url.rstrip("/") or "https://api.github.com"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=30.0, verify=s.httpx_verify) as client:
            login = ""
            if token:
                resp = client.get(f"{base}/user", headers=headers)
                resp.raise_for_status()
                login = resp.json().get("login", "")

            reachable: list[str] = []
            unreachable: list[str] = []
            for repo in repos:
                r = client.get(f"{base}/repos/{repo}", headers=headers)
                (reachable if r.status_code == 200 else unreachable).append(repo)
    except httpx.HTTPStatusError as exc:
        return _result(False, _http_error_message(exc))
    except httpx.HTTPError as exc:
        return _result(False, "اتصال برقرار نشد — API URL را بررسی کن", str(exc)[:200])

    if repos and unreachable:
        return _result(
            False,
            f"{len(unreachable)} repo در دسترس نیست",
            "غیرقابل‌دسترس: " + ", ".join(unreachable),
        )
    parts = []
    if login:
        parts.append(f"کاربر: {login}")
    if reachable:
        parts.append(f"{len(reachable)} repo در دسترس")
    return _result(True, "اتصال برقرار شد", " · ".join(parts))


def _test_azure(s: Settings) -> dict[str, Any]:
    from qa_agent.tools.azure_devops import AzureDevOpsClient

    if not s.azure_devops_configured:
        return _not_configured("Organization، Project یا PAT تنظیم نشده")
    try:
        data = AzureDevOpsClient(s)._get("_apis/git/repositories")
    except httpx.HTTPStatusError as exc:
        return _result(False, _http_error_message(exc))
    except httpx.HTTPError as exc:
        return _result(False, "اتصال برقرار نشد — Base URL/Org/Project را بررسی کن", str(exc)[:200])

    count = len(data.get("value", [])) if isinstance(data, dict) else 0
    return _result(True, "اتصال برقرار شد", f"{count} repository در پروژه")


def _test_openapi(s: Settings) -> dict[str, Any]:
    from qa_agent.tools.openapi import OpenAPIClient, _flatten_operations, _spec_name

    specs = s.openapi_specs_list
    if not specs:
        return _not_configured("هیچ spec ای تنظیم نشده")

    client = OpenAPIClient(s)
    loaded = 0
    ops = 0
    failed: list[str] = []
    for location in specs:
        spec = client.load_spec(location)
        if not spec:
            failed.append(location)
            continue
        loaded += 1
        ops += len(_flatten_operations(spec, _spec_name(location, spec), location))

    if not loaded:
        return _result(False, "هیچ spec ای بارگذاری نشد", "ناموفق: " + ", ".join(failed))
    if failed:
        return _result(
            False,
            f"{len(failed)} از {len(specs)} spec بارگذاری نشد",
            "ناموفق: " + ", ".join(failed),
        )
    return _result(True, "اتصال برقرار شد", f"{loaded} spec · {ops} endpoint")


_TESTS: dict[str, Callable[[Settings], dict[str, Any]]] = {
    "outline": _test_outline,
    "confluence": _test_confluence,
    "github": _test_github,
    "azure": _test_azure,
    "openapi": _test_openapi,
}


def test_connector(name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the live connectivity check for ``name`` and return a result dict."""
    test = _TESTS.get(name)
    if test is None:
        raise KeyError(name)
    settings = settings_with_overrides(overrides)
    try:
        result = test(settings)
    except Exception as exc:  # defensive — never let a probe crash the request
        result = _result(False, "تست اتصال با خطا مواجه شد", str(exc)[:200])
    result["connector"] = name
    return result
