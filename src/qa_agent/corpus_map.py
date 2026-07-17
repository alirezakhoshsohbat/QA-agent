"""Deterministic corpus map: a one-page, layer-grouped table of contents over
corpus/docs/ that researchers read before searching. Built without any LLM
call, so it is free to regenerate on every index run.
"""

from __future__ import annotations

from pathlib import Path

from qa_agent.config import Settings, get_settings
from qa_agent.retrieval import get_corpus_index

_MAP_FILENAME = "corpus_map.md"
_LAYER_ORDER = ("frontend", "bff", "backend", "e2e", "unknown")


def corpus_map_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.corpus_index_dir / _MAP_FILENAME


def build_corpus_map(settings: Settings | None = None) -> Path:
    """Write corpus_map.md grouped by layer: title, doc id, short excerpt."""
    from qa_agent.tools.outline import _layer_hint

    settings = settings or get_settings()
    index = get_corpus_index(settings)
    index.ensure()

    grouped: dict[str, list[str]] = {layer: [] for layer in _LAYER_ORDER}
    for doc in sorted(index.docs.values(), key=lambda d: d.title.lower()):
        layer = _layer_hint(doc.title, doc.excerpt)
        excerpt = doc.excerpt[:120].strip()
        line = f"- **{doc.title}** (id=`{doc.doc_id}`)"
        if excerpt:
            line += f" — {excerpt}"
        grouped.setdefault(layer, []).append(line)

    lines = [
        "# Corpus map",
        "",
        f"{len(index.docs)} documents in corpus/docs, grouped by layer. Use the",
        "doc ids with `outline_get_document`; use searches only for what this",
        "map does not answer.",
        "",
    ]
    for layer in _LAYER_ORDER:
        entries = grouped.get(layer) or []
        if not entries:
            continue
        lines.append(f"## {layer} ({len(entries)})")
        lines.extend(entries)
        lines.append("")

    path = corpus_map_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_corpus_map(settings: Settings | None = None, max_chars: int = 5000) -> str:
    """Corpus map contents; builds it on first use. Empty corpus → empty string."""
    settings = settings or get_settings()
    path = corpus_map_path(settings)
    if not path.exists():
        try:
            build_corpus_map(settings)
        except OSError:
            return ""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [truncated — search for anything not listed]"
    return text
