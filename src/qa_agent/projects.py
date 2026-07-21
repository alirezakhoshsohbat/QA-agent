"""Multi-project workspace: isolated prefs, cache, corpus, and output per project."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECTS_ROOT = Path("projects")
REGISTRY_NAME = "registry.json"
DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "پیش‌فرض"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    created_at: str

    @property
    def root(self) -> Path:
        return PROJECTS_ROOT / self.id

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created_at": self.created_at}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _registry_path() -> Path:
    return PROJECTS_ROOT / REGISTRY_NAME


def _read_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {"active_id": DEFAULT_PROJECT_ID, "projects": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_id": DEFAULT_PROJECT_ID, "projects": []}
    if not isinstance(payload, dict):
        return {"active_id": DEFAULT_PROJECT_ID, "projects": []}
    projects = payload.get("projects")
    if not isinstance(projects, list):
        projects = []
    active = str(payload.get("active_id") or DEFAULT_PROJECT_ID)
    return {"active_id": active, "projects": projects}


def _write_registry(registry: dict[str, Any]) -> None:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = _registry_path()
    path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _ensure_project_dirs(project_id: str) -> Path:
    root = PROJECTS_ROOT / project_id
    for sub in (".cache", "corpus", "output", "graphify-out"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def slugify_project_id(name: str) -> str:
    text = (name or "").strip().lower()
    # Keep ASCII slug; fall back for Persian / non-latin names.
    ascii_part = _SLUG_RE.sub("-", text).strip("-")
    if ascii_part and ascii_part.replace("-", "").isalnum():
        base = ascii_part[:48]
    else:
        base = f"project-{_utcnow_iso().replace(':', '').replace('-', '')[:14]}"
    candidate = base
    n = 2
    existing = {p["id"] for p in _read_registry().get("projects", []) if isinstance(p, dict)}
    while candidate in existing or candidate in {REGISTRY_NAME}:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _project_from_dict(raw: dict[str, Any]) -> Project | None:
    pid = str(raw.get("id") or "").strip()
    if not pid or pid == REGISTRY_NAME or "/" in pid or "\\" in pid or ".." in pid:
        return None
    name = str(raw.get("name") or pid).strip() or pid
    created = str(raw.get("created_at") or _utcnow_iso())
    return Project(id=pid, name=name, created_at=created)


def list_projects() -> list[Project]:
    ensure_projects()
    out: list[Project] = []
    for raw in _read_registry().get("projects", []):
        if isinstance(raw, dict):
            project = _project_from_dict(raw)
            if project:
                out.append(project)
    return out


def get_active_project_id() -> str:
    ensure_projects()
    registry = _read_registry()
    active = str(registry.get("active_id") or DEFAULT_PROJECT_ID)
    ids = {p.id for p in list_projects()}
    if active not in ids:
        active = DEFAULT_PROJECT_ID if DEFAULT_PROJECT_ID in ids else next(iter(ids), DEFAULT_PROJECT_ID)
        registry["active_id"] = active
        _write_registry(registry)
    return active


def get_project(project_id: str | None = None) -> Project:
    ensure_projects()
    pid = (project_id or get_active_project_id()).strip()
    for project in list_projects():
        if project.id == pid:
            _ensure_project_dirs(project.id)
            return project
    raise KeyError(f"Project not found: {pid}")


def project_root(project_id: str | None = None) -> Path:
    return get_project(project_id).root


def preferences_path_for(project_id: str | None = None) -> Path:
    if project_id is None:
        return get_project().root / "preferences.json"
    return PROJECTS_ROOT / project_id / "preferences.json"


def activate_project(project_id: str) -> Project:
    project = get_project(project_id)
    registry = _read_registry()
    registry["active_id"] = project.id
    _write_registry(registry)
    return project


def create_project(name: str, *, project_id: str | None = None) -> Project:
    ensure_projects()
    clean_name = (name or "").strip() or "پروژه جدید"
    pid = (project_id or slugify_project_id(clean_name)).strip()
    if not pid or pid == REGISTRY_NAME or "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError("Invalid project id")
    registry = _read_registry()
    existing = {
        str(p.get("id"))
        for p in registry.get("projects", [])
        if isinstance(p, dict)
    }
    if pid in existing:
        raise ValueError(f"Project already exists: {pid}")
    project = Project(id=pid, name=clean_name, created_at=_utcnow_iso())
    _ensure_project_dirs(project.id)
    prefs = preferences_path_for(project.id)
    if not prefs.exists():
        prefs.write_text("{}\n", encoding="utf-8")
    projects = list(registry.get("projects") or [])
    projects.append(project.to_dict())
    registry["projects"] = projects
    if not registry.get("active_id"):
        registry["active_id"] = project.id
    _write_registry(registry)
    return project


def rename_project(project_id: str, name: str) -> Project:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Project name is required")
    registry = _read_registry()
    updated: Project | None = None
    projects: list[dict[str, Any]] = []
    for raw in registry.get("projects", []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id")) == project_id:
            raw = {**raw, "name": clean_name}
            updated = _project_from_dict(raw)
        projects.append(raw)
    if updated is None:
        raise KeyError(f"Project not found: {project_id}")
    registry["projects"] = projects
    _write_registry(registry)
    return updated


def delete_project(project_id: str) -> None:
    ensure_projects()
    registry = _read_registry()
    projects = [
        p
        for p in registry.get("projects", [])
        if isinstance(p, dict) and str(p.get("id")) != project_id
    ]
    if len(projects) == len(registry.get("projects", [])):
        raise KeyError(f"Project not found: {project_id}")
    if len(projects) == 0:
        raise ValueError("Cannot delete the last project")
    if project_id == get_active_project_id():
        raise ValueError("Cannot delete the active project; switch first")
    registry["projects"] = projects
    _write_registry(registry)
    root = PROJECTS_ROOT / project_id
    if root.exists():
        shutil.rmtree(root)


def apply_project_paths(settings: Any, project: Project) -> Any:
    """Overlay corpus/output/cache/graphify paths onto a Settings instance."""
    root = project.root
    _ensure_project_dirs(project.id)
    return settings.model_copy(
        update={
            "qa_agent_project_id": project.id,
            "qa_agent_project_root": root,
            "qa_agent_output_dir": root / "output",
            "qa_agent_corpus_dir": root / "corpus",
            "qa_agent_graphify_out": root / "graphify-out",
        }
    )


def _move_if_exists(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for child in list(src.iterdir()):
            target = dest / child.name
            if target.exists():
                continue
            shutil.move(str(child), str(target))
        try:
            if src.exists() and not any(src.iterdir()):
                src.rmdir()
        except OSError:
            pass
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    shutil.move(str(src), str(dest))


def _migrate_legacy_into_default() -> None:
    """One-shot: fold root-level cache/corpus/output into projects/default/."""
    default_root = _ensure_project_dirs(DEFAULT_PROJECT_ID)
    legacy_prefs = Path(".cache") / "ui_preferences.json"
    new_prefs = default_root / "preferences.json"
    if legacy_prefs.exists() and not new_prefs.exists():
        new_prefs.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_prefs), str(new_prefs))
    elif not new_prefs.exists():
        new_prefs.write_text("{}\n", encoding="utf-8")

    # Move connector / query caches under the project .cache (except prefs already moved).
    legacy_cache = Path(".cache")
    dest_cache = default_root / ".cache"
    if legacy_cache.exists() and legacy_cache.is_dir():
        for child in list(legacy_cache.iterdir()):
            if child.name == "ui_preferences.json":
                continue
            _move_if_exists(child, dest_cache / child.name)
        # Remove empty legacy .cache if possible
        try:
            if legacy_cache.exists() and not any(legacy_cache.iterdir()):
                legacy_cache.rmdir()
        except OSError:
            pass

    for name in ("corpus", "output", "graphify-out"):
        _move_if_exists(Path(name), default_root / name)


def ensure_projects() -> None:
    """Create registry + default project; migrate legacy global dirs once."""
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    registry = _read_registry()
    projects_raw = [
        p for p in registry.get("projects", []) if isinstance(p, dict) and p.get("id")
    ]
    if not projects_raw:
        _migrate_legacy_into_default()
        project = Project(
            id=DEFAULT_PROJECT_ID,
            name=DEFAULT_PROJECT_NAME,
            created_at=_utcnow_iso(),
        )
        _ensure_project_dirs(project.id)
        prefs = preferences_path_for(project.id)
        if not prefs.exists():
            prefs.write_text("{}\n", encoding="utf-8")
        registry = {
            "active_id": DEFAULT_PROJECT_ID,
            "projects": [project.to_dict()],
        }
        _write_registry(registry)
        return

    # Ensure every registered project has dirs; fix missing active.
    for raw in projects_raw:
        pid = str(raw.get("id") or "")
        if pid:
            _ensure_project_dirs(pid)
            prefs = PROJECTS_ROOT / pid / "preferences.json"
            if not prefs.exists():
                prefs.write_text("{}\n", encoding="utf-8")
    ids = {str(p.get("id")) for p in projects_raw}
    active = str(registry.get("active_id") or "")
    if active not in ids:
        registry["active_id"] = (
            DEFAULT_PROJECT_ID if DEFAULT_PROJECT_ID in ids else next(iter(ids))
        )
        registry["projects"] = projects_raw
        _write_registry(registry)
