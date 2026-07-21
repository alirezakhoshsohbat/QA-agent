"""Hard-disable of connectors: no tools, prompt steps, or index sync when off."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qa_agent.agent import _build_prompt, create_qa_agent
from qa_agent.config import Settings
from qa_agent.indexing import run_index


def _base_settings(**kwargs) -> Settings:
    defaults = dict(
        _env_file=None,
        llm_api_key="test-key",
        llm_base_url="http://localhost/v1",
        qa_agent_model_profile="router",
        qa_agent_outline_subagent=True,
        qa_agent_github_subagent=True,
        qa_agent_confluence_subagent=True,
        qa_agent_azure_subagent=True,
        qa_agent_openapi_subagent=True,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_build_prompt_omits_disabled_connectors():
    settings = _base_settings(
        qa_agent_outline_subagent=False,
        qa_agent_github_subagent=False,
        github_repos="owner/repo",
    )
    prompt = _build_prompt("login flow", 500, settings)
    assert "outline-researcher" not in prompt
    assert "github-researcher" not in prompt
    assert "use GitHub tools directly" not in prompt.lower()
    assert "Outline/corpus tools" not in prompt
    assert "write_feature_file" in prompt


def test_build_prompt_includes_enabled_outline():
    settings = _base_settings(qa_agent_outline_subagent=True)
    prompt = _build_prompt("search pane", 500, settings)
    assert "outline-researcher" in prompt


@patch("qa_agent.agent.create_deep_agent")
@patch("qa_agent.agent.create_research_model")
@patch("qa_agent.agent.create_chat_model")
@patch("qa_agent.agent.resolve_model_ref", return_value="openai:gpt-4.1")
def test_create_qa_agent_skips_disabled_tools(
    _resolve,
    _chat,
    _research,
    mock_deep,
):
    mock_deep.return_value = MagicMock(name="agent")
    settings = _base_settings(
        qa_agent_outline_subagent=False,
        qa_agent_github_subagent=False,
        qa_agent_confluence_subagent=False,
        qa_agent_azure_subagent=False,
        qa_agent_openapi_subagent=False,
        qa_agent_output_dir=Path("./output"),
    )
    with (
        patch("qa_agent.agent.create_outline_tools") as outline,
        patch("qa_agent.agent.create_github_tools") as github,
        patch("qa_agent.agent.create_confluence_tools") as confluence,
        patch("qa_agent.agent.create_azure_tools") as azure,
        patch("qa_agent.agent.create_openapi_tools") as openapi,
        patch("qa_agent.agent.create_graphify_tools", return_value=[]),
        patch("qa_agent.agent.create_output_tools", return_value=[]),
    ):
        create_qa_agent(settings=settings)
        outline.assert_not_called()
        github.assert_not_called()
        confluence.assert_not_called()
        azure.assert_not_called()
        openapi.assert_not_called()
        kwargs = mock_deep.call_args.kwargs
        assert kwargs["subagents"] == []


def test_run_index_ignores_sync_when_connector_off(tmp_path: Path):
    settings = _base_settings(
        qa_agent_outline_subagent=False,
        qa_agent_github_subagent=False,
        qa_agent_corpus_dir=tmp_path / "corpus",
        qa_agent_graphify_out=tmp_path / "graphify-out",
        qa_agent_project_root=tmp_path,
    )
    with (
        patch("qa_agent.indexing.sync_outline_docs") as sync_outline,
        patch("qa_agent.indexing.sync_github_code") as sync_github,
    ):
        run_index(
            outline_sync=True,
            github_clone=True,
            with_graph=False,
            settings=settings,
        )
        sync_outline.assert_not_called()
        sync_github.assert_not_called()
