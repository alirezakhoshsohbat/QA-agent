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
    index_options: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = "queued"
    phase: str = "queued"
    progress: int = 0
    message: str = "در انتظار شروع…"
    result: dict[str, Any] | None = None
    error: str | None = None
    activities: list[dict[str, Any]] = field(default_factory=list)
    partial_gherkin: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class JobStore:
    """Thread-safe in-memory job registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = threading.Lock()
        self._activity_count: dict[str, int] = {}

    def create(self, query: str, budget: int, dry_run: bool = False) -> GenerationJob:
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            query=query,
            budget=budget,
            dry_run=dry_run,
            kind="generate",
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
    ) -> GenerationJob:
        parts: list[str] = []
        if outline_sync:
            parts.append("Outline")
        if github_clone:
            parts.append("GitHub")
        if with_graph:
            parts.append("Graph")
        query = "Index: " + (", ".join(parts) if parts else "corpus only")
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            query=query,
            budget=0,
            dry_run=False,
            kind="index",
            index_options={
                "outline_sync": outline_sync,
                "github_clone": github_clone,
                "with_graph": with_graph,
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
            "partial_gherkin": job.partial_gherkin,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }


job_store = JobStore()
