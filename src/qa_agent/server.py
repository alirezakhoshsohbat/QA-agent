from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from qa_agent.agent import run_generate, run_generate_from_pr
from qa_agent.config import Settings, get_settings
from qa_agent.indexing import run_index
from qa_agent.jobs import job_store
from qa_agent.models.llm import describe_model_endpoint, validate_model_credentials
from qa_agent.tools.graphify import graph_exists
from qa_agent.models.schemas import GeneratedFeature, slugify
from qa_agent.cost import CostTracker
from qa_agent.tools.output import (
    DRY_RUN_TEMPLATE,
    resolve_gherkin_from_output,
    validate_gherkin,
    write_feature_files,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
STATIC_DIR = WEB_DIR / "static"


def _user_facing_error(exc: BaseException, *, fallback: str) -> str:
    """Short Persian/English UI message; keep gateways' raw dump out of the toast."""
    text = str(exc) or ""
    lowered = text.lower()
    if (
        "429" in text
        or "rate limit" in lowered
        or "rate_limit" in lowered
        or "请求数限制" in text
        or "too many requests" in lowered
    ):
        return (
            "سقف درخواست LLM پر شد (۴۲۹). یک دقیقه صبر کنید و دوباره بزنید. "
            "در Settings → پایداری، LLM RPM را حدود ۱۸ نگه دارید (۰ = بدون محدودیت)."
        )
    if "content-blocked" in lowered or "content_blocked" in lowered:
        return (
            "گیت‌وی LLM این درخواست را مسدود کرد (content-blocked) — معمولاً "
            "به‌خاطر محتوای PR/مستندات در مرحله research. "
            "مدل research را عوض کنید، کانکتورهای غیرضروری را خاموش کنید، "
            "یا کوئری را کوتاه‌تر/عمومی‌تر بزنید و دوباره تلاش کنید."
        )
    if (
        "new_api_panic" in lowered
        or "panic detected" in lowered
        or ("error code: 500" in lowered and "new-api" in lowered)
        or ("error code: 500" in lowered and "openaierror" in lowered)
    ):
        return (
            "گیت‌وی LLM دچار خطای داخلی شد (500 / new-api panic) — مشکل از سمت پروکسی است، "
            "نه از QA Agent. چند لحظه صبر کنید و دوباره بزنید؛ اگر تکرار شد مدل یا "
            "base URL دیگری امتحان کنید."
        )
    return (text[:500] if text else fallback)

app = FastAPI(title="QA Agent", version="0.1.0", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class GenerateRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=4000)
    budget: int = Field(default=1500, ge=200, le=8000)
    dry_run: bool = False


class IndexRequest(BaseModel):
    outline_sync: bool = False
    github_clone: bool = False
    with_graph: bool = False
    confluence_sync: bool = False
    azure_clone: bool = False
    openapi_sync: bool = False


class PRGenerateRequest(BaseModel):
    repo: str = Field(..., min_length=3, max_length=200)
    number: int = Field(..., ge=1)
    budget: int = Field(default=1500, ge=200, le=8000)


def _settings(project_id: str | None = None) -> Settings:
    return get_settings(project_id)


def _active_project_id() -> str:
    from qa_agent.projects import get_active_project_id

    return get_active_project_id()


def _emit_activity(job_id: str, activity: dict[str, Any]) -> None:
    from qa_agent.activity import activity_from_event

    if activity.get("type") == "partial_gherkin":
        gherkin = activity.get("gherkin", "")
        job_store.set_partial_gherkin(job_id, gherkin)
        job_store.append_log(
            job_id,
            f"partial_gherkin ({len(gherkin)} chars)",
            level="debug",
            source="agent",
            data={"preview": gherkin[:400]},
        )
        return

    # Triage / escalation / other non-tool events → normalize to a timeline card.
    if activity.get("type") in {"triage", "escalation"}:
        card = activity_from_event(activity)
        if card:
            job_store.append_activity(job_id, card)
            _log_activity(job_id, card, raw=activity)
        return

    phase = activity.get("phase")
    if activity.get("status") == "running" or activity.get("phase") == "start":
        activity["phase"] = (
            phase if phase and phase not in {"start", "end"} else activity.get("phase", "running")
        )
        job_store.append_activity(job_id, activity)
        _log_activity(job_id, activity)
    elif activity.get("status") == "done" or activity.get("phase") == "end":
        job_store.complete_activity(
            job_id,
            activity.get("tool", ""),
            result_preview=activity.get("result_preview", ""),
            detail=activity.get("detail", ""),
        )
        _log_activity(job_id, activity)


def _log_activity(
    job_id: str,
    activity: dict[str, Any],
    *,
    raw: dict[str, Any] | None = None,
) -> None:
    """Mirror activity cards into the developer console with technical detail."""
    tool = activity.get("tool") or activity.get("type") or "event"
    status = activity.get("status") or "info"
    title = activity.get("title") or tool
    level = "debug" if status == "done" else "info"
    if status == "running":
        arrow = "▶"
        level = "info"
    elif status == "done":
        arrow = "◀"
        level = "debug"
    else:
        arrow = "·"

    payload: dict[str, Any] = {
        "tool": tool,
        "status": status,
        "phase": activity.get("phase"),
        "category": activity.get("category"),
        "detail": activity.get("detail") or "",
        "result_preview": activity.get("result_preview") or "",
    }
    if raw:
        for key in ("complexity", "generate_role", "reason", "source", "type"):
            if key in raw and raw[key] not in (None, ""):
                payload[key] = raw[key]

    job_store.append_log(
        job_id,
        f"{arrow} {title}",
        level=level,
        source=str(activity.get("category") or "tool"),
        data=payload,
    )


def _run_index_job(
    job_id: str,
    *,
    outline_sync: bool,
    github_clone: bool,
    with_graph: bool,
    confluence_sync: bool = False,
    azure_clone: bool = False,
    openapi_sync: bool = False,
) -> None:
    job = job_store.get(job_id)
    settings = _settings(job.project_id if job and job.project_id else None)
    try:
        job_store.update(
            job_id,
            status="running",
            phase="init",
            progress=5,
            message="شروع ایندکس…",
        )
        job_store.append_log(
            job_id,
            "index job started",
            level="info",
            source="job",
            data={
                "outline_sync": outline_sync,
                "github_clone": github_clone,
                "with_graph": with_graph,
                "confluence_sync": confluence_sync,
                "azure_clone": azure_clone,
                "openapi_sync": openapi_sync,
                "project_id": job.project_id if job else "",
            },
        )

        def on_activity(activity: dict[str, Any]) -> None:
            _emit_activity(job_id, activity)

        result = run_index(
            outline_sync=outline_sync,
            github_clone=github_clone,
            with_graph=with_graph,
            confluence_sync=confluence_sync,
            azure_clone=azure_clone,
            openapi_sync=openapi_sync,
            settings=settings,
            on_activity=on_activity,
        )
        job_store.append_log(
            job_id,
            "index job completed",
            level="info",
            source="job",
            data=result,
        )
        job_store.update(
            job_id,
            status="completed",
            phase="done",
            progress=100,
            message="ایندکس کامل شد.",
            result=result,
        )
    except Exception as exc:
        import logging
        import traceback

        logging.getLogger("qa_agent").exception("index job %s failed", job_id)
        job_store.append_log(
            job_id,
            f"index job failed: {exc}",
            level="error",
            source="job",
            data=traceback.format_exc(),
        )
        job_store.update(
            job_id,
            status="failed",
            phase="error",
            progress=100,
            message="ایندکس ناموفق بود.",
            error=str(exc),
        )


def _run_job(job_id: str) -> None:
    job = job_store.get(job_id)
    if not job:
        return

    settings = _settings(job.project_id or None)

    try:
        job_store.update(
            job_id,
            status="running",
            phase="init",
            progress=5,
            message="آماده‌سازی Agent…",
        )
        job_store.append_log(
            job_id,
            "generate job started",
            level="info",
            source="job",
            data={
                "query": job.query,
                "budget": job.budget,
                "dry_run": job.dry_run,
                "project_id": job.project_id,
            },
        )

        if job.dry_run:
            dry_activity = {
                "id": "dry1",
                "tool": "write_feature_file",
                "category": "output",
                "icon": "write",
                "title": "نوشتن template آزمایشی",
                "detail": job.query,
                "status": "running",
                "phase": "writing",
                "timestamp": time.time(),
            }
            job_store.append_activity(job_id, dry_activity)
            _log_activity(job_id, dry_activity)
            cost = CostTracker(budget=job.budget)
            template = DRY_RUN_TEMPLATE.format(query=job.query.replace('"', '\\"'))
            feature = GeneratedFeature(slug=slugify(job.query), feature_content=template, query=job.query)
            paths = write_feature_files(feature, settings.qa_agent_output_dir, cost)
            gherkin = template
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            cost_data = json.loads(paths["cost"].read_text(encoding="utf-8"))
            job_store.set_partial_gherkin(job_id, gherkin)
            done_activity = {
                **dry_activity,
                "status": "done",
                "result_preview": "فایل .feature ذخیره شد",
            }
            job_store.complete_activity(
                job_id,
                "write_feature_file",
                result_preview="فایل .feature ذخیره شد",
                detail=job.query,
            )
            _log_activity(job_id, done_activity)
            job_store.append_log(
                job_id,
                "dry-run completed",
                level="info",
                source="job",
                data={"feature_path": str(paths["feature"]), "cost": cost_data},
            )
            job_store.update(
                job_id,
                status="completed",
                phase="done",
                progress=100,
                message="قالب آزمایشی نوشته شد.",
                result={
                    "response": gherkin,
                    "gherkin": gherkin,
                    "feature_path": str(paths["feature"]),
                    "meta": meta,
                    "cost": cost_data,
                    "validation_errors": meta.get("validation_errors", validate_gherkin(gherkin)),
                    "quality_warnings": meta.get("quality_warnings", []),
                    "coverage": meta.get("coverage", {}),
                },
            )
            return

        missing = validate_model_credentials(settings)
        if missing:
            raise RuntimeError(f"Missing configuration: {', '.join(missing)}")

        job_store.update(
            job_id,
            phase="research",
            progress=5,
            message="Agent شروع به کار کرد…",
        )

        def on_activity(activity: dict[str, Any]) -> None:
            _emit_activity(job_id, activity)

        output = run_generate(
            job.query,
            settings=settings,
            budget=job.budget,
            on_activity=on_activity,
        )
        _finalize_generate(job_id, settings, output)
    except Exception as exc:
        import logging
        import traceback

        logging.getLogger("qa_agent").exception("generate job %s failed", job_id)
        detail = f"{exc}\n{traceback.format_exc()}"
        job_store.append_log(
            job_id,
            f"generate job failed: {exc}",
            level="error",
            source="job",
            data=detail,
        )
        job_store.update(
            job_id,
            status="failed",
            phase="error",
            progress=100,
            message=_user_facing_error(exc, fallback="تولید ناموفق بود."),
            error=detail[:12000],
        )


def _finalize_generate(job_id: str, settings: Settings, output: dict[str, Any]) -> None:
    """Attribute the newest feature file + metadata to a completed generation job."""
    response = output["response"]
    write_blocked = bool(output.get("write_blocked"))
    write_attempt = int(output.get("write_attempt") or 0)
    blocked_output = output.get("blocked_output")

    feature_files = sorted(
        settings.qa_agent_output_dir.glob("*.feature"),
        key=lambda path: path.stat().st_mtime,
    )
    latest_feature = feature_files[-1] if feature_files else None
    feature_path = str(latest_feature) if latest_feature else None
    meta: dict[str, Any] = {}
    cost_obj = output.get("cost")
    if cost_obj is None:
        cost_data = {}
    elif hasattr(cost_obj, "model_dump"):
        cost_data = cost_obj.model_dump()
    elif isinstance(cost_obj, dict):
        cost_data = cost_obj
    else:
        cost_data = {}

    if latest_feature:
        stem = latest_feature.stem
        meta_path = settings.qa_agent_output_dir / f"{stem}.meta.json"
        cost_path = settings.qa_agent_output_dir / f"{stem}.cost.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if cost_path.exists():
            cost_data = json.loads(cost_path.read_text(encoding="utf-8"))

    gherkin, validation_errors = resolve_gherkin_from_output(
        response=response,
        feature_path=latest_feature,
    )
    if write_blocked and not latest_feature:
        job = job_store.get(job_id)
        gherkin = job.partial_gherkin if job and job.partial_gherkin else gherkin

    completion_message = (
        "تولید بلاک شد — اعتبارسنجی بعد از تلاش‌ها ناموفق بود."
        if write_blocked and not latest_feature
        else "تست‌کیس‌های Gherkin آماده‌اند."
    )

    job_store.append_log(
        job_id,
        "generate job completed" if not (write_blocked and not latest_feature) else "generate job blocked",
        level="warn" if write_blocked and not latest_feature else "info",
        source="job",
        data={
            "feature_path": feature_path,
            "write_blocked": write_blocked,
            "write_attempt": write_attempt,
            "validation_errors": validation_errors,
            "gherkin_chars": len(gherkin or ""),
            "cost": cost_data,
        },
    )
    job_store.update(
        job_id,
        status="completed",
        phase="done",
        progress=100,
        message=completion_message,
        result={
            "response": response,
            "gherkin": gherkin,
            "feature_path": feature_path,
            "meta": meta,
            "cost": cost_data,
            "validation_errors": validation_errors,
            "quality_warnings": meta.get("quality_warnings", []),
            "coverage": meta.get("coverage", {}),
            "write_blocked": write_blocked,
            "write_attempt": write_attempt,
            "blocked_output": blocked_output,
        },
    )


def _run_pr_job(job_id: str, repo: str, number: int) -> None:
    job = job_store.get(job_id)
    settings = _settings(job.project_id if job and job.project_id else None)
    try:
        budget = job.budget if job else settings.qa_agent_token_budget
        job_store.update(job_id, status="running", phase="init", progress=5, message="دریافت PR…")

        missing = validate_model_credentials(settings)
        if missing:
            raise RuntimeError(f"Missing configuration: {', '.join(missing)}")

        job_store.update(job_id, phase="research", progress=5, message="تحلیل PR…")

        def on_activity(activity: dict[str, Any]) -> None:
            _emit_activity(job_id, activity)

        output = run_generate_from_pr(
            repo,
            number,
            settings=settings,
            budget=budget,
            on_activity=on_activity,
        )
        _finalize_generate(job_id, settings, output)
    except Exception as exc:
        import logging
        import traceback

        logging.getLogger("qa_agent").exception("pr job %s failed", job_id)
        job_store.append_log(
            job_id,
            f"pr job failed: {exc}",
            level="error",
            source="job",
            data=traceback.format_exc(),
        )
        job_store.update(
            job_id,
            status="failed",
            phase="error",
            progress=100,
            message=_user_facing_error(exc, fallback="تولید از PR ناموفق بود."),
            error=str(exc),
        )


@app.get("/")
async def index_page() -> FileResponse:
    html = WEB_DIR / "index.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="Web UI not found")
    return FileResponse(html)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    from qa_agent.projects import get_project

    settings = _settings()
    missing = validate_model_credentials(settings)
    research = describe_model_endpoint(settings, "research")
    generate = describe_model_endpoint(settings, "generate")
    nano = describe_model_endpoint(settings, "nano")
    pro = describe_model_endpoint(settings, "pro")

    graph_path = settings.qa_agent_graphify_out / "graph.json"
    graph_nodes = 0
    graph_note = ""
    if graph_path.exists():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph_nodes = len(graph.get("nodes", []))
            graph_note = graph.get("note", "")
        except (json.JSONDecodeError, OSError):
            graph_note = "Graph file unreadable"

    project_name = ""
    if settings.qa_agent_project_id:
        try:
            project_name = get_project(settings.qa_agent_project_id).name
        except KeyError:
            project_name = settings.qa_agent_project_id

    return {
        "profile": settings.qa_agent_model_profile,
        "project_id": settings.qa_agent_project_id,
        "project_name": project_name,
        "research_model": settings.active_research_model(),
        "generate_model": settings.active_generate_model(),
        "nano_model": nano["model"],
        "pro_model": pro["model"],
        "embedding_model": settings.qa_agent_embedding_model or None,
        "research_endpoint": research["base_url"],
        "generate_endpoint": generate["base_url"],
        "credentials_ok": len(missing) == 0,
        "missing_credentials": missing,
        "github_repos": settings.github_repos_list,
        "outline_configured": bool(settings.outline_api_key),
        "outline_subagent": settings.qa_agent_outline_subagent,
        "github_subagent": settings.qa_agent_github_subagent,
        "confluence_configured": settings.confluence_configured,
        "confluence_subagent": settings.qa_agent_confluence_subagent,
        "azure_configured": settings.azure_devops_configured,
        "azure_repos": settings.azure_devops_repos_list,
        "azure_subagent": settings.qa_agent_azure_subagent,
        "openapi_configured": settings.openapi_configured,
        "openapi_specs": settings.openapi_specs_list,
        "openapi_subagent": settings.qa_agent_openapi_subagent,
        "token_budget_default": settings.qa_agent_token_budget,
        "output_dir": str(settings.qa_agent_output_dir),
        "graph": {
            "exists": graph_exists(settings.qa_agent_graphify_out),
            "nodes": graph_nodes,
            "note": graph_note,
        },
    }


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    """Effective UI-managed preferences (env defaults overlaid with saved prefs).

    Secret fields are masked; accompanying ``*_set`` flags tell the UI whether
    a value is configured.
    """
    from qa_agent.preferences import load_preferences, preferences_snapshot
    from qa_agent.projects import get_project

    settings = _settings()
    project = get_project(settings.qa_agent_project_id or None)
    return {
        "project_id": project.id,
        "project_name": project.name,
        "values": preferences_snapshot(settings),
        "overrides": {
            k: v
            for k, v in load_preferences(project.id).items()
            if k not in {
                "llm_api_key",
                "qa_agent_embedding_api_key",
                "outline_api_key",
                "github_token",
                "confluence_api_token",
                "azure_devops_pat",
                "openapi_token",
            }
        },
        "secrets_note": (
            "Leave a secret blank to keep the current value. Typing a new key "
            "replaces it. Base URLs and connector toggles apply on the next run."
        ),
    }


class ConnectorTestRequest(BaseModel):
    """Optional unsaved form values to test a connector before persisting."""

    values: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/connectors/{name}/test")
async def api_test_connector(name: str, body: ConnectorTestRequest | None = None) -> dict[str, Any]:
    from qa_agent.connectors import CONNECTORS, test_connector

    if name not in CONNECTORS:
        raise HTTPException(status_code=404, detail="Unknown connector")
    overrides = body.values if body else {}
    return test_connector(name, overrides)


class SettingsUpdate(BaseModel):
    """Partial update for UI-managed preference fields."""

    values: dict[str, Any] = Field(default_factory=dict)


@app.put("/api/settings")
async def api_put_settings(body: SettingsUpdate) -> dict[str, Any]:
    from qa_agent.preferences import (
        preferences_snapshot,
        save_preferences,
    )

    if not body.values:
        raise HTTPException(status_code=400, detail="No settings provided")
    settings = _settings()
    project_id = settings.qa_agent_project_id or _active_project_id()
    save_preferences(body.values, current_settings=settings, project_id=project_id)
    settings = _settings(project_id)
    return {
        "ok": True,
        "project_id": project_id,
        "values": preferences_snapshot(settings),
    }


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class ProjectRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


@app.get("/api/projects")
async def api_list_projects() -> dict[str, Any]:
    from qa_agent.projects import get_active_project_id, list_projects

    active = get_active_project_id()
    projects = [
        {**p.to_dict(), "active": p.id == active}
        for p in list_projects()
    ]
    return {"active_id": active, "projects": projects}


@app.post("/api/projects")
async def api_create_project(body: ProjectCreate) -> dict[str, Any]:
    from qa_agent.projects import activate_project, create_project

    try:
        project = create_project(body.name.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activate_project(project.id)
    return {"ok": True, "project": {**project.to_dict(), "active": True}}


@app.post("/api/projects/{project_id}/activate")
async def api_activate_project(project_id: str) -> dict[str, Any]:
    from qa_agent.projects import activate_project

    try:
        project = activate_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "project": {**project.to_dict(), "active": True}}


@app.patch("/api/projects/{project_id}")
async def api_rename_project(project_id: str, body: ProjectRename) -> dict[str, Any]:
    from qa_agent.projects import get_active_project_id, rename_project

    try:
        project = rename_project(project_id, body.name.strip())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "project": {**project.to_dict(), "active": project.id == get_active_project_id()},
    }


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str) -> dict[str, Any]:
    from qa_agent.projects import delete_project

    try:
        delete_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/runs")
async def api_runs(limit: int = 50) -> dict[str, Any]:
    settings = _settings()
    output_dir = settings.qa_agent_output_dir
    if not output_dir.exists():
        return {"runs": []}

    runs: list[dict[str, Any]] = []
    for feature_path in sorted(output_dir.glob("*.feature"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(runs) >= limit:
            break
        stem = feature_path.stem
        meta_path = output_dir / f"{stem}.meta.json"
        cost_path = output_dir / f"{stem}.cost.json"
        meta: dict[str, Any] = {}
        cost: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        if cost_path.exists():
            try:
                cost = json.loads(cost_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        runs.append(
            {
                "id": stem,
                "feature_path": str(feature_path),
                "query": meta.get("query", stem),
                "timestamp": meta.get("timestamp", ""),
                "sources_count": len(meta.get("sources", [])),
                "validation_errors": meta.get("validation_errors", []),
                "quality_warnings": meta.get("quality_warnings", []),
                "coverage": meta.get("coverage", {}),
                "estimated_usd": cost.get("estimated_usd"),
                "modified_at": feature_path.stat().st_mtime,
            }
        )
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str) -> dict[str, Any]:
    settings = _settings()
    feature_path = settings.qa_agent_output_dir / f"{run_id}.feature"
    if not feature_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    meta_path = settings.qa_agent_output_dir / f"{run_id}.meta.json"
    cost_path = settings.qa_agent_output_dir / f"{run_id}.cost.json"
    gherkin = feature_path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    cost: dict[str, Any] = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if cost_path.exists():
        cost = json.loads(cost_path.read_text(encoding="utf-8"))

    return {
        "id": run_id,
        "gherkin": gherkin,
        "meta": meta,
        "cost": cost,
        "validation_errors": meta.get("validation_errors") or validate_gherkin(gherkin),
        "quality_warnings": meta.get("quality_warnings", []),
        "coverage": meta.get("coverage", {}),
    }


@app.post("/api/generate")
async def api_generate(body: GenerateRequest) -> dict[str, str]:
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    project_id = _active_project_id()
    job = job_store.create(
        query=query,
        budget=body.budget,
        dry_run=body.dry_run,
        project_id=project_id,
    )
    thread = threading.Thread(target=_run_job, args=(job.id,), daemon=True)
    thread.start()
    return {"job_id": job.id}


@app.post("/api/generate/pr")
async def api_generate_pr(body: PRGenerateRequest) -> dict[str, str]:
    project_id = _active_project_id()
    job = job_store.create(
        query=f"PR {body.repo}#{body.number}",
        budget=body.budget,
        project_id=project_id,
    )
    thread = threading.Thread(
        target=_run_pr_job,
        args=(job.id, body.repo.strip(), body.number),
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}


@app.get("/api/runs/{run_id}/trace.{ext}")
async def api_run_trace(run_id: str, ext: str) -> Any:
    from fastapi.responses import HTMLResponse, PlainTextResponse

    from qa_agent.reporting import build_traceability, traceability_csv, traceability_html

    settings = _settings()
    feature_path = settings.qa_agent_output_dir / f"{run_id}.feature"
    if not feature_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    if ext not in {"csv", "html"}:
        raise HTTPException(status_code=400, detail="Format must be csv or html")

    content = feature_path.read_text(encoding="utf-8")
    meta_path = settings.qa_agent_output_dir / f"{run_id}.meta.json"
    checklist: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        checklist = meta.get("acceptance_checklist", [])
        sources = meta.get("sources", [])

    rows = build_traceability(content, checklist, sources)
    if ext == "csv":
        return PlainTextResponse(traceability_csv(rows), media_type="text/csv")
    return HTMLResponse(traceability_html(rows, title=f"Traceability — {run_id}"))


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str) -> dict[str, Any]:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_store.to_dict(job)


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str) -> StreamingResponse:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    def event_stream():
        last_payload = ""
        while True:
            current = job_store.get(job_id)
            if not current:
                break
            payload = json.dumps(job_store.to_dict(current), ensure_ascii=False)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if current.status in {"completed", "failed"}:
                break
            time.sleep(0.4)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/index")
async def api_index(body: IndexRequest) -> dict[str, str]:
    project_id = _active_project_id()
    job = job_store.create_index(
        outline_sync=body.outline_sync,
        github_clone=body.github_clone,
        with_graph=body.with_graph,
        confluence_sync=body.confluence_sync,
        azure_clone=body.azure_clone,
        openapi_sync=body.openapi_sync,
        project_id=project_id,
    )
    thread = threading.Thread(
        target=_run_index_job,
        kwargs={
            "job_id": job.id,
            "outline_sync": body.outline_sync,
            "github_clone": body.github_clone,
            "with_graph": body.with_graph,
            "confluence_sync": body.confluence_sync,
            "azure_clone": body.azure_clone,
            "openapi_sync": body.openapi_sync,
        },
        daemon=True,
    )
    thread.start()
    return {"job_id": job.id}
