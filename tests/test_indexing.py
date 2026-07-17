from pathlib import Path

from qa_agent.config import Settings
from qa_agent.indexing import run_index


def test_run_index_empty_corpus(tmp_path):
    settings = type("S", (), {})()
    settings.qa_agent_corpus_dir = tmp_path / "corpus"
    settings.qa_agent_graphify_out = tmp_path / "graphify-out"
    settings.corpus_docs_dir = settings.qa_agent_corpus_dir / "docs"
    settings.corpus_code_dir = settings.qa_agent_corpus_dir / "code"
    settings.outline_api_key = ""
    settings.outline_base_url = "https://outline.test/api"
    settings.github_repos_list = getattr(settings, "github_repos_list", [])
    settings.cache_dir = tmp_path / ".cache"
    settings.corpus_index_dir = settings.qa_agent_corpus_dir / "index"
    settings.qa_agent_embedding_model = ""
    settings.qa_agent_embedding_base_url = ""
    settings.qa_agent_embedding_api_key = ""
    settings.llm_base_url = ""
    settings.llm_api_key = ""

    result = run_index(outline_sync=False, github_clone=False, settings=settings)
    assert result["graph_built"] is True
    assert (tmp_path / "graphify-out" / "graph.json").exists()


def test_github_repos_list():
    # _env_file=None isolates the test from the developer's local .env
    settings = Settings(_env_file=None, github_repos="chimney-ai/web,chimney-ai/bot")
    assert settings.github_repos_list == ["chimney-ai/web", "chimney-ai/bot"]

    settings = Settings(_env_file=None, github_repo="chimney-ai/web")
    assert settings.github_repos_list == ["chimney-ai/web"]
