from qa_agent.config import Settings
from qa_agent.models.llm import resolve_model_ref
from qa_agent.query_expansion import parse_expansion_response
from qa_agent.triage import heuristic_triage, parse_triage_response, triage_request


def test_heuristic_simple_request():
    decision = heuristic_triage("تست فلوی لاگین کاربر")
    assert decision.complexity == "simple"
    assert decision.generate_role == "generate"
    assert decision.source == "heuristic"


def test_heuristic_complex_request():
    decision = heuristic_triage(
        "تمام تست کیس های حیاتی و مهم برای search pane و login و پرداخت و "
        "profile رو کامل و end-to-end بنویس، همه حالت ها"
    )
    assert decision.complexity == "complex"
    assert decision.generate_role == "pro"


def test_parse_triage_response_variants():
    assert parse_triage_response('{"complexity": "complex", "reason": "x"}') == (
        "complex",
        "x",
    )
    assert parse_triage_response(
        '```json\n{"complexity": "simple", "reason": "one flow"}\n```'
    ) == ("simple", "one flow")
    assert parse_triage_response("not json") is None
    assert parse_triage_response('{"complexity": "huge"}') is None


def test_triage_disabled_returns_standard():
    settings = Settings(_env_file=None, qa_agent_triage=False)
    decision = triage_request("anything", settings)
    assert decision.source == "disabled"
    assert decision.generate_role == "generate"


def test_triage_falls_back_to_heuristic_without_credentials():
    settings = Settings(_env_file=None, qa_agent_triage=True)
    decision = triage_request("تست فلوی لاگین", settings)
    assert decision.source == "heuristic"


def test_parse_expansion_response():
    raw = '{"terms": ["authentication", "login flow"], "entities": ["search-pane"]}'
    terms = parse_expansion_response(raw)
    # entities come first (exact feature names), then concept terms
    assert terms[0] == "search-pane"
    assert "authentication" in terms
    assert parse_expansion_response("garbage") == []


def test_nano_and_pro_roles_fall_back():
    settings = Settings(
        _env_file=None,
        qa_agent_model_profile="router",
        qa_agent_research_model="cheap-model",
        qa_agent_generate_model="big-model",
    )
    assert resolve_model_ref(settings, "nano") == "cheap-model"
    assert resolve_model_ref(settings, "pro") == "big-model"

    settings = Settings(
        _env_file=None,
        qa_agent_model_profile="router",
        qa_agent_research_model="cheap-model",
        qa_agent_generate_model="big-model",
        qa_agent_nano_model="tiny-model",
        qa_agent_pro_model="huge-model",
    )
    assert resolve_model_ref(settings, "nano") == "tiny-model"
    assert resolve_model_ref(settings, "pro") == "huge-model"
