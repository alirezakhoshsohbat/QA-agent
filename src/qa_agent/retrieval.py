"""Hybrid corpus retrieval: BM25 + optional embeddings, fused with RRF.

The corpus index is built from ``corpus/docs/*.md`` and persisted under
``corpus/index/``. BM25 is pure Python (no extra dependencies). Embeddings are
optional: they activate only when ``QA_AGENT_EMBEDDING_MODEL`` is configured,
and any embedding failure silently degrades to BM25-only so retrieval never
breaks a run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from qa_agent.config import Settings, get_settings

# Words that add no search value (queries are often Persian "make test cases for …").
SEARCH_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "test", "tests", "case",
    "cases", "feature", "features", "flow", "flows", "user", "make", "create",
    "generate", "verify", "should", "when", "then", "about", "please", "write",
    "برام", "بساز", "تست", "کیس", "کیس‌ها", "راجب", "مورد", "برای", "یک", "که",
    "باید", "بنویس", "سناریو", "سناریوها", "درباره", "درمورد", "کن", "بده",
}

_BM25_K1 = 1.5
_BM25_B = 0.75
_RRF_K = 60
_INDEX_VERSION = 1
_DOC_TEXT_LIMIT = 8000
_EMBED_TEXT_LIMIT = 1800


def tokenize(text: str) -> list[str]:
    """Unicode-aware tokens, lowercased, stopwords removed."""
    text = unicodedata.normalize("NFKC", text.lower())
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    return [t for t in tokens if len(t) >= 2 and t not in SEARCH_STOP_WORDS]


def _doc_id_from_filename(path: Path) -> str:
    full = re.search(
        r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", path.name, re.IGNORECASE
    )
    if full:
        return full.group(1)
    short = re.search(r"-([0-9a-f]{8})\.md$", path.name, re.IGNORECASE)
    return short.group(1) if short else path.stem


def _title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("# ").strip() or fallback
    return fallback


@dataclass
class IndexedDoc:
    key: str  # filename stem — stable per corpus file
    doc_id: str
    title: str
    path: str
    length: int
    term_freqs: dict[str, int]
    excerpt: str = ""
    embedding: list[float] | None = None


@dataclass
class CorpusIndex:
    """BM25 (+ optional embedding) index over a docs directory."""

    docs_dir: Path
    index_dir: Path
    docs: dict[str, IndexedDoc] = field(default_factory=dict)
    doc_freqs: dict[str, int] = field(default_factory=dict)
    avg_length: float = 0.0
    fingerprint: str = ""

    @property
    def index_path(self) -> Path:
        return self.index_dir / "corpus_index.json"

    def _corpus_fingerprint(self) -> str:
        entries = sorted(
            f"{p.name}:{p.stat().st_mtime_ns}"
            for p in self.docs_dir.glob("*.md")
            if p.is_file()
        )
        raw = f"v{_INDEX_VERSION}|" + "|".join(entries)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def build(self, embedder: EmbeddingClient | None = None) -> int:
        self.docs.clear()
        self.doc_freqs.clear()
        if not self.docs_dir.exists():
            return 0

        total_length = 0
        for path in sorted(self.docs_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")[:_DOC_TEXT_LIMIT]
            except OSError:
                continue
            title = _title_from_text(text, path.stem)
            tokens = tokenize(f"{title}\n{text}")
            if not tokens:
                continue
            freqs: dict[str, int] = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            body = text.split("\n", 1)[1] if "\n" in text else ""
            excerpt = " ".join(body.split())[:200]
            doc = IndexedDoc(
                key=path.stem,
                doc_id=_doc_id_from_filename(path),
                title=title,
                path=str(path),
                length=len(tokens),
                term_freqs=freqs,
                excerpt=excerpt,
            )
            self.docs[doc.key] = doc
            total_length += doc.length
            for term in freqs:
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        self.avg_length = total_length / len(self.docs) if self.docs else 0.0
        self.fingerprint = self._corpus_fingerprint()

        if embedder and embedder.enabled and self.docs:
            keys = list(self.docs.keys())
            texts = [
                f"{self.docs[k].title}\n{self.docs[k].excerpt}"[:_EMBED_TEXT_LIMIT]
                for k in keys
            ]
            vectors = embedder.embed_texts(texts)
            if vectors and len(vectors) == len(keys):
                for key, vector in zip(keys, vectors):
                    self.docs[key].embedding = vector

        self.save()
        return len(self.docs)

    def save(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _INDEX_VERSION,
            "fingerprint": self.fingerprint,
            "avg_length": self.avg_length,
            "doc_freqs": self.doc_freqs,
            "docs": {
                key: {
                    "doc_id": d.doc_id,
                    "title": d.title,
                    "path": d.path,
                    "length": d.length,
                    "term_freqs": d.term_freqs,
                    "excerpt": d.excerpt,
                    "embedding": d.embedding,
                }
                for key, d in self.docs.items()
            },
        }
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def load(self) -> bool:
        if not self.index_path.exists():
            return False
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if payload.get("version") != _INDEX_VERSION:
            return False
        self.fingerprint = payload.get("fingerprint", "")
        self.avg_length = float(payload.get("avg_length", 0.0))
        self.doc_freqs = {str(k): int(v) for k, v in payload.get("doc_freqs", {}).items()}
        self.docs = {}
        for key, raw in payload.get("docs", {}).items():
            self.docs[key] = IndexedDoc(
                key=key,
                doc_id=str(raw.get("doc_id", key)),
                title=str(raw.get("title", key)),
                path=str(raw.get("path", "")),
                length=int(raw.get("length", 0)),
                term_freqs={str(t): int(c) for t, c in raw.get("term_freqs", {}).items()},
                excerpt=str(raw.get("excerpt", "")),
                embedding=raw.get("embedding"),
            )
        return bool(self.docs)

    def ensure(self, embedder: EmbeddingClient | None = None) -> None:
        """Load the persisted index; rebuild if missing or corpus changed."""
        if self.load() and self.fingerprint == self._corpus_fingerprint():
            return
        self.build(embedder)

    def bm25_ranking(self, query_tokens: list[str]) -> list[tuple[str, float]]:
        if not self.docs or not query_tokens:
            return []
        n_docs = len(self.docs)
        scores: dict[str, float] = {}
        for term in query_tokens:
            df = self.doc_freqs.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for key, doc in self.docs.items():
                tf = doc.term_freqs.get(term, 0)
                if tf == 0:
                    continue
                denom = tf + _BM25_K1 * (
                    1 - _BM25_B + _BM25_B * doc.length / (self.avg_length or 1.0)
                )
                scores[key] = scores.get(key, 0.0) + idf * (tf * (_BM25_K1 + 1)) / denom
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    def embedding_ranking(
        self, query_vector: list[float] | None
    ) -> list[tuple[str, float]]:
        if not query_vector:
            return []
        scored: list[tuple[str, float]] = []
        for key, doc in self.docs.items():
            if not doc.embedding:
                continue
            score = _cosine(query_vector, doc.embedding)
            if score > 0:
                scored.append((key, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def rrf_fuse(rankings: list[list[str]], k: int = _RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion of ranked key lists."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
    return fused


class EmbeddingClient:
    """OpenAI-compatible /embeddings client; disabled when no model configured."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.qa_agent_embedding_model.strip()
        self.base_url = (
            self.settings.qa_agent_embedding_base_url.strip()
            or self.settings.llm_base_url.strip()
        )
        self.api_key = (
            self.settings.qa_agent_embedding_api_key.strip()
            or self.settings.llm_api_key.strip()
        )

    @property
    def enabled(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)

    def embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        if not self.enabled or not texts:
            return None
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                data = response.json().get("data", [])
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            return None
        if len(data) != len(texts):
            return None
        # API may return items out of order; sort by index.
        data = sorted(data, key=lambda item: item.get("index", 0))
        return [item.get("embedding") or [] for item in data]

    def embed_query(self, query: str) -> list[float] | None:
        vectors = self.embed_texts([query[:_EMBED_TEXT_LIMIT]])
        return vectors[0] if vectors else None


_INDEX_CACHE: dict[str, CorpusIndex] = {}


def get_corpus_index(
    settings: Settings | None = None, docs_dir: Path | None = None
) -> CorpusIndex:
    settings = settings or get_settings()
    docs_dir = docs_dir or settings.corpus_docs_dir
    index_dir = (
        settings.corpus_index_dir
        if docs_dir == settings.corpus_docs_dir
        else docs_dir.parent / "index"
    )
    key = str(docs_dir.resolve())
    index = _INDEX_CACHE.get(key)
    if index is None:
        index = CorpusIndex(docs_dir=docs_dir, index_dir=index_dir)
        _INDEX_CACHE[key] = index
    return index


def hybrid_search(
    query: str,
    settings: Settings | None = None,
    *,
    docs_dir: Path | None = None,
    extra_terms: list[str] | None = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """BM25 + optional embedding search over the local corpus, RRF-fused.

    ``extra_terms`` (e.g. LLM-expanded English terms for a Persian query) are
    appended to the BM25 token stream and boost recall across languages.
    """
    settings = settings or get_settings()
    embedder = EmbeddingClient(settings)
    index = get_corpus_index(settings, docs_dir)
    index.ensure(embedder if embedder.enabled else None)
    if not index.docs:
        return []

    query_tokens = tokenize(query)
    for term in extra_terms or []:
        query_tokens.extend(tokenize(term))
    query_tokens = list(dict.fromkeys(query_tokens))

    bm25 = index.bm25_ranking(query_tokens)
    rankings = [[key for key, _ in bm25]]

    if embedder.enabled:
        embed_query_text = query
        if extra_terms:
            embed_query_text += "\n" + " ".join(extra_terms)
        vector_ranking = index.embedding_ranking(embedder.embed_query(embed_query_text))
        if vector_ranking:
            rankings.append([key for key, _ in vector_ranking])

    fused = rrf_fuse(rankings)
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)

    results: list[dict[str, Any]] = []
    for key, score in ordered[:max_results]:
        doc = index.docs.get(key)
        if not doc:
            continue
        results.append(
            {
                "id": doc.doc_id,
                "title": doc.title,
                "path": doc.path,
                "excerpt": doc.excerpt,
                "score": round(score, 4),
                "origins": len(rankings),
            }
        )
    return results
