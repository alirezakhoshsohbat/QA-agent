from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

JobStatus = Literal["queued", "running", "completed", "failed"]
JobKind = Literal["generate", "index"]


@dataclass
class GenerationJob:
    id: str
    query: str
    budget: int
    dry_run: bool
    kind: JobKind = "generate"
    project_id: str = ""
    index_options: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = "queued"
    phase: str = "queued"
    progress: int = 0
    message: str = "در انتظار شروع…"
    result: dict[str, Any] | None = None
    error: str | None = None
    activities: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    partial_gherkin: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_MAX_JOB_LOGS = 800
_MAX_LOG_DATA_CHARS = 4000


def _clip_log_data(data: Any) -> Any:
    """Keep developer log payloads bounded so the UI stays responsive."""
    if data is None:
        return None
    if isinstance(data, str):
        if len(data) <= _MAX_LOG_DATA_CHARS:
            return data
        return data[: _MAX_LOG_DATA_CHARS - 1] + "…"
    if isinstance(data, (int, float, bool)):
        return data
    if isinstance(data, dict):
        clipped: dict[str, Any] = {}
        for key, value in list(data.items())[:40]:
            clipped[str(key)] = _clip_log_data(value)
        if len(data) > 40:
            clipped["…"] = f"+{len(data) - 40} keys"
        return clipped
    if isinstance(data, (list, tuple)):
        items = [_clip_log_data(item) for item in list(data)[:40]]
        if len(data) > 40:
            items.append(f"… +{len(data) - 40} items")
        return items
    text = str(data)
    if len(text) <= _MAX_LOG_DATA_CHARS:
        return text
    return text[: _MAX_LOG_DATA_CHARS - 1] + "…"


class JobStore:
    """Thread-safe in-memory job registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = threading.Lock()
        self._activity_count: dict[str, int] = {}

    def create(
        self,
        query: str,
        budget: int,
        dry_run: bool = False,
        *,
        project_id: str = "",
    ) -> GenerationJob:
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            query=query,
            budget=budget,
            dry_run=dry_run,
            kind="generate",
            project_id=project_id,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._activity_count[job.id] = 0
        return job

    def create_index(
        self,
        *,
        outline_sync: bool,
        github_clone: bool,
        with_graph: bool,
        confluence_sync: bool = False,
        azure_clone: bool = False,
        openapi_sync: bool = False,
        project_id: str = "",
    ) -> GenerationJob:
        parts: list[str] = []
        if outline_sync:
            parts.append("Outline")
        if confluence_sync:
            parts.append("Confluence")
        if openapi_sync:
            parts.append("OpenAPI")
        if github_clone:
            parts.append("GitHub")
        if azure_clone:
            parts.append("Azure")
        if with_graph:
            parts.append("Graph")
        query = "Index: " + (", ".join(parts) if parts else "corpus only")
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            query=query,
            budget=0,
            dry_run=False,
            kind="index",
            project_id=project_id,
            index_options={
                "outline_sync": outline_sync,
                "github_clone": github_clone,
                "with_graph": with_graph,
                "confluence_sync": confluence_sync,
                "azure_clone": azure_clone,
                "openapi_sync": openapi_sync,
            },
        )
        with self._lock:
            self._jobs[job.id] = job
            self._activity_count[job.id] = 0
        return job

    def get(self, job_id: str) -> GenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def append_activity(self, job_id: str, activity: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.activities.append(activity)
            self._bump_progress(job_id, job, activity)

    def append_log(
        self,
        job_id: str,
        message: str,
        *,
        level: str = "info",
        source: str = "job",
        data: Any = None,
    ) -> None:
        """Append a developer-mode console line for the Web UI."""
        entry = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.time(),
            "level": level if level in {"debug", "info", "warn", "error"} else "info",
            "source": source or "job",
            "message": str(message)[:500],
            "data": _clip_log_data(data),
        }
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.logs.append(entry)
            if len(job.logs) > _MAX_JOB_LOGS:
                job.logs = job.logs[-_MAX_JOB_LOGS:]

    def complete_activity(
        self,
        job_id: str,
        tool: str,
        *,
        result_preview: str = "",
        detail: str = "",
    ) -> None:
        """Mark the most recent running activity for a tool as done."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for activity in reversed(job.activities):
                if activity.get("tool") == tool and activity.get("status") == "running":
                    activity["status"] = "done"
                    if result_preview:
                        activity["result_preview"] = result_preview
                    if detail:
                        activity["detail"] = detail
                    self._bump_progress(job_id, job, activity)
                    return
            # Fallback: append standalone done entry
            from qa_agent.activity import TOOL_META, INDEX_STEP_META

            meta = TOOL_META.get(tool) or INDEX_STEP_META.get(tool) or {
                "category": "tool",
                "icon": "tool",
                "title": tool.replace("_", " "),
            }
            job.activities.append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "tool": tool,
                    "category": meta["category"],
                    "icon": meta["icon"],
                    "title": meta["title"],
                    "detail": detail,
                    "result_preview": result_preview,
                    "status": "done",
                    "phase": "running",
                    "timestamp": time.time(),
                }
            )
            self._bump_progress(job_id, job, job.activities[-1])

    def _bump_progress(self, job_id: str, job: GenerationJob, activity: dict[str, Any]) -> None:
        count = self._activity_count.get(job_id, 0) + 1
        self._activity_count[job_id] = count
        phase = activity.get("phase")
        if phase and phase not in {"start", "end"}:
            job.phase = phase
        if activity.get("status") == "running":
            job.message = f"{activity.get('title', 'در حال کار')}…"
        elif activity.get("result_preview"):
            job.message = activity["result_preview"]
        job.progress = min(95, 5 + count * 3)
        if job.kind == "index":
            if activity.get("tool") == "index_finalize" and activity.get("status") == "done":
                job.progress = 98
                job.phase = "indexing"
        elif activity.get("tool") == "write_feature_file" and activity.get("status") == "done":
            job.progress = 98
            job.phase = "writing"

    def set_partial_gherkin(self, job_id: str, gherkin: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.partial_gherkin = gherkin

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        phase: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> GenerationJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if status is not None:
                job.status = status
            if phase is not None:
                job.phase = phase
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            if status in {"completed", "failed"}:
                job.finished_at = time.time()
            return job

    def list_recent(self, limit: int = 20) -> list[GenerationJob]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def to_dict(self, job: GenerationJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "kind": job.kind,
            "project_id": job.project_id,
            "query": job.query,
            "budget": job.budget,
            "dry_run": job.dry_run,
            "index_options": job.index_options,
            "status": job.status,
            "phase": job.phase,
            "progress": job.progress,
            "message": job.message,
            "result": job.result,
            "error": job.error,
            "activities": job.activities,
            "logs": job.logs,
            "partial_gherkin": job.partial_gherkin,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }


job_store = JobStore()
