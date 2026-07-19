from qa_agent.config import Settings
from qa_agent.models.llm import (
    describe_model_endpoint,
    parse_model_ref,
    resolve_model_ref,
    validate_model_credentials,
)


def test_parse_model_ref():
    assert parse_model_ref("openai:gpt-4.1") == ("openai", "gpt-4.1")
    assert parse_model_ref("kimi:moonshot-v1-32k") == ("moonshot", "moonshot-v1-32k")
    assert parse_model_ref("glm:glm-5.2") == ("zhipu", "glm-5.2")
    assert parse_model_ref("gpt-4.1-mini") == ("openai", "gpt-4.1-mini")


def test_hybrid_profile_defaults():
    settings = Settings(
        qa_agent_model_profile="hybrid",
        openai_api_key="sk-test",
        moonshot_api_key="sk-test",
    )
    assert settings.active_research_model() == "kimi:moonshot-v1-32k"
    assert settings.active_generate_model() == "openai:gpt-4.1"


def test_router_uses_plain_model_names():
    settings = Settings(
        qa_agent_model_profile="router",
        llm_api_key="sk-test",
        llm_base_url="https://router.example/v1",
        qa_agent_research_model="moonshotai/kimi-k2.7-code",
        qa_agent_generate_model="openai/gpt-4.1",
    )
    assert settings.active_research_model() == "moonshotai/kimi-k2.7-code"
    assert settings.active_generate_model() == "openai/gpt-4.1"
    assert settings.uses_router() is True


def test_custom_profile():
    settings = Settings(
        qa_agent_model_profile="custom",
        qa_agent_research_model="openai:gpt-4o-mini",
        qa_agent_generate_model="glm:glm-5.2",
    )
    assert settings.active_research_model() == "openai:gpt-4o-mini"
    assert settings.active_generate_model() == "glm:glm-5.2"


def test_validate_credentials_hybrid():
    settings = Settings(
        qa_agent_model_profile="hybrid",
        openai_api_key="",
        moonshot_api_key="",
        llm_api_key="",
        llm_base_url="",
        openai_base_url="",
    )
    missing = validate_model_credentials(settings)
    assert any("OPENAI_API_KEY" in m for m in missing)
    assert any("MOONSHOT_API_KEY" in m for m in missing)


def test_validate_credentials_hybrid_ok():
    settings = Settings(
        qa_agent_model_profile="hybrid",
        openai_api_key="sk-test",
        moonshot_api_key="sk-test",
    )
    assert validate_model_credentials(settings) == []


def test_glm_profile():
    settings = Settings(qa_agent_model_profile="glm")
    assert "glm" in settings.active_research_model()
    assert "glm" in settings.active_generate_model()


def test_router_profile():
    settings = Settings(
        qa_agent_model_profile="router",
        llm_api_key="sk-test",
        llm_base_url="https://router.example/v1",
        qa_agent_research_model="kimi-model",
        qa_agent_generate_model="gpt-model",
    )
    assert settings.active_research_model() == "kimi-model"
    assert settings.active_generate_model() == "gpt-model"


def test_global_base_url_applied_to_openai():
    settings = Settings(
        qa_agent_model_profile="custom",
        qa_agent_research_model="openai:gpt-4.1-mini",
        qa_agent_generate_model="openai:gpt-4.1",
        openai_api_key="sk-test",
        llm_base_url="https://my-router.example/v1",
    )
    research = describe_model_endpoint(settings, "research")
    assert research["base_url"] == "https://my-router.example/v1"


def test_role_base_url_overrides_global():
    settings = Settings(
        qa_agent_model_profile="router",
        llm_api_key="sk-test",
        llm_base_url="https://global-router.example/v1",
        qa_agent_research_model="kimi-model",
        qa_agent_generate_model="gpt-model",
        qa_agent_research_base_url="https://research-router.example/v1",
        qa_agent_generate_base_url="https://generate-router.example/v1",
    )
    assert describe_model_endpoint(settings, "research")["base_url"] == "https://research-router.example/v1"
    assert describe_model_endpoint(settings, "generate")["base_url"] == "https://generate-router.example/v1"
    assert describe_model_endpoint(settings, "research")["model"] == "kimi-model"


def test_openai_base_url_over_provider_default():
    settings = Settings(
        qa_agent_model_profile="balanced",
        openai_api_key="sk-test",
        openai_base_url="https://custom-openai-proxy.example/v1",
    )
    endpoint = describe_model_endpoint(settings, "generate")
    assert endpoint["base_url"] == "https://custom-openai-proxy.example/v1"


def test_validate_router_requires_base_url():
    settings = Settings(qa_agent_model_profile="router", llm_api_key="", llm_base_url="")
    missing = validate_model_credentials(settings)
    assert "LLM_BASE_URL" in missing
    assert "LLM_API_KEY" not in missing


def test_validate_router_local_without_api_key():
    """Blank API key + base URL = local OpenAI-compatible server."""
    settings = Settings(
        qa_agent_model_profile="router",
        llm_api_key="",
        llm_base_url="http://192.168.10.222:11434/v1",
        qa_agent_research_model="qwen2.5",
        qa_agent_generate_model="qwen2.5",
    )
    assert validate_model_credentials(settings) == []


def test_validate_custom_local_without_api_key():
    settings = Settings(
        qa_agent_model_profile="custom",
        qa_agent_research_model="openai:local-model",
        qa_agent_generate_model="openai:local-model",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_api_key="",
        openai_api_key="",
    )
    assert validate_model_credentials(settings) == []


def test_agentrouter_header_helpers():
    from qa_agent.models.llm import (
        agentrouter_default_headers,
        is_agentrouter_base_url,
    )

    assert is_agentrouter_base_url("https://agentrouter.org/v1") is True
    assert is_agentrouter_base_url("https://openrouter.ai/api/v1") is False
    headers = agentrouter_default_headers()
    assert headers["User-Agent"].startswith("QwenCode/")
    assert headers["X-Stainless-Lang"] == "js"


def test_agentrouter_disables_streaming(monkeypatch):
    """AgentRouter SSE is unreliable; ChatOpenAI must not stream."""
    captured: dict = {}

    def fake_init_chat_model(**kwargs):
        captured.update(kwargs)
        return object()

    from qa_agent.models import llm as llm_mod

    monkeypatch.setattr(llm_mod, "init_chat_model", fake_init_chat_model)
    settings = Settings(
        qa_agent_model_profile="router",
        llm_api_key="sk-test",
        llm_base_url="https://agentrouter.org/v1",
        qa_agent_research_model="claude-opus-4-6",
        qa_agent_generate_model="claude-opus-4-6",
    )
    llm_mod.create_chat_model("claude-opus-4-6", settings, role="generate")
    assert captured.get("disable_streaming") is True
    assert captured.get("stream_usage") is False
    assert captured.get("default_headers", {}).get("X-Stainless-Lang") == "js"
