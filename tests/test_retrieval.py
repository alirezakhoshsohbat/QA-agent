from pathlib import Path

from qa_agent.retrieval import (
    CorpusIndex,
    EmbeddingClient,
    hybrid_search,
    rrf_fuse,
    tokenize,
)


def _make_settings(tmp_path: Path):
    settings = type("S", (), {})()
    settings.qa_agent_corpus_dir = tmp_path / "corpus"
    settings.corpus_docs_dir = settings.qa_agent_corpus_dir / "docs"
    settings.corpus_index_dir = settings.qa_agent_corpus_dir / "index"
    settings.cache_dir = tmp_path / ".cache"
    settings.qa_agent_embedding_model = ""
    settings.qa_agent_embedding_base_url = ""
    settings.qa_agent_embedding_api_key = ""
    settings.llm_base_url = ""
    settings.llm_api_key = ""
    settings.qa_agent_query_expansion = False
    return settings


def _write_docs(docs_dir: Path) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "authentication-f793d571.md").write_text(
        "# Authentication\n\nLogin flow with OTP verification. POST /api/auth/login "
        "accepts phone_number and returns a session token.",
        encoding="utf-8",
    )
    (docs_dir / "search-pane-1234abcd.md").write_text(
        "# Search Pane\n\nThe search pane filters property listings by city and "
        "price range. GET /api/search?city=... returns paginated results.",
        encoding="utf-8",
    )
    (docs_dir / "backup-db-job-461ed071.md").write_text(
        "# Backup DB Job\n\nNightly cron job dumps the database to S3.",
        encoding="utf-8",
    )


def test_tokenize_strips_stopwords_and_persian():
    tokens = tokenize("برام راجب Login Flow تست کیس بساز")
    assert "login" in tokens
    assert "بساز" not in tokens
    assert "تست" not in tokens


def test_bm25_ranks_relevant_doc_first(tmp_path):
    settings = _make_settings(tmp_path)
    _write_docs(settings.corpus_docs_dir)

    hits = hybrid_search("login authentication OTP", settings, max_results=3)
    assert hits, "expected at least one hit"
    assert "authentication" in hits[0]["title"].lower()


def test_extra_terms_bridge_persian_to_english(tmp_path):
    settings = _make_settings(tmp_path)
    _write_docs(settings.corpus_docs_dir)

    # Pure-Persian query with no token overlap; expanded English terms rescue it.
    hits = hybrid_search(
        "احراز هویت",
        settings,
        extra_terms=["authentication", "login"],
        max_results=3,
    )
    assert hits
    assert "authentication" in hits[0]["title"].lower()


def test_index_persists_and_reloads(tmp_path):
    settings = _make_settings(tmp_path)
    _write_docs(settings.corpus_docs_dir)

    index = CorpusIndex(
        docs_dir=settings.corpus_docs_dir, index_dir=settings.corpus_index_dir
    )
    built = index.build()
    assert built == 3
    assert index.index_path.exists()

    fresh = CorpusIndex(
        docs_dir=settings.corpus_docs_dir, index_dir=settings.corpus_index_dir
    )
    assert fresh.load() is True
    assert set(fresh.docs) == set(index.docs)
    assert fresh.doc_freqs == index.doc_freqs


def test_rrf_fusion_prefers_agreement():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
    assert fused["a"] > fused["c"]
    assert fused["b"] > fused["c"]
    assert fused["b"] > fused["d"]


def test_embedding_client_disabled_without_model(tmp_path):
    settings = _make_settings(tmp_path)
    client = EmbeddingClient(settings)
    assert client.enabled is False
    assert client.embed_texts(["hello"]) is None


def test_hybrid_search_empty_corpus(tmp_path):
    settings = _make_settings(tmp_path)
    settings.corpus_docs_dir.mkdir(parents=True, exist_ok=True)
    assert hybrid_search("anything", settings) == []
