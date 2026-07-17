from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM — OpenAI
    openai_api_key: str = ""
    openai_base_url: str = ""

    # LLM — universal router / gateway (overrides when provider key missing)
    llm_api_key: str = ""
    llm_base_url: str = ""

    # LLM — per-role base URL overrides (optional)
    qa_agent_research_base_url: str = ""
    qa_agent_generate_base_url: str = ""

    # LLM — Moonshot / Kimi (OpenAI-compatible)
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    # LLM — Z.ai / GLM (OpenAI-compatible)
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"

    # LLM — OpenRouter (optional unified gateway)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Model selection — router mode (default): one LLM_BASE_URL + LLM_API_KEY,
    # different model name per role via QA_AGENT_RESEARCH_MODEL / QA_AGENT_GENERATE_MODEL
    qa_agent_model_profile: str = "router"
    qa_agent_model: str = "openai/gpt-4.1"
    qa_agent_research_model: str = "moonshotai/kimi-k2.7-code"
    qa_agent_generate_model: str = "openai/gpt-4.1"

    # Optional extra tiers. Nano: tiny/cheap model for query expansion, triage,
    # and reranking (falls back to the research model when empty). Pro: strongest
    # model, used when triage flags a complex request or a blocked write
    # escalates (falls back to the generate model when empty — escalation is a
    # no-op in that case).
    qa_agent_nano_model: str = ""
    qa_agent_pro_model: str = ""

    # Smart retrieval. Query expansion rewrites Persian/vague requests into
    # English search terms via the nano model (cached; falls back to heuristics).
    # Embeddings activate hybrid (BM25+vector) corpus search when a model is set;
    # endpoint/key default to LLM_BASE_URL/LLM_API_KEY.
    qa_agent_query_expansion: bool = True
    qa_agent_embedding_model: str = ""
    qa_agent_embedding_base_url: str = ""
    qa_agent_embedding_api_key: str = ""

    # Complexity triage before generation: nano model (heuristic fallback)
    # decides whether the request needs the pro tier upfront.
    qa_agent_triage: bool = True
    # Escalate a still-blocked write to the pro model for one extra attempt.
    qa_agent_escalation: bool = True

    # Retries with exponential backoff on transient LLM errors (429 rate limits,
    # 5xx). Important on rate-limited free tiers where a multi-agent run bursts
    # many calls; 0 disables retries.
    qa_agent_llm_max_retries: int = 5

    # Persistence — how hard the agent works before giving up on a run. The agent
    # keeps re-deciding and self-correcting until it lands a clean, fully-covered
    # feature file or hits these bounded ceilings (bounded so a genuinely
    # impossible ask can't loop forever / burn unlimited tokens).
    #   max_write_attempts: BLOCKED write→fix→rewrite cycles per model tier.
    #   max_reprompts: total autonomous nudges (blocked retries + stall + refine).
    qa_agent_max_write_attempts: int = 4
    qa_agent_max_reprompts: int = 8
    # After a structurally-valid write, spend up to this many extra rounds asking
    # the agent to remove vague/abstract scenarios so "done" means "concrete &
    # covered", not merely "parses". 0 disables quality refinement.
    qa_agent_quality_refine_rounds: int = 2

    # Sub-agents — toggle from Web UI. When disabled, the orchestrator skips
    # that researcher and the corresponding tools stay available for direct use.
    qa_agent_outline_subagent: bool = True
    qa_agent_github_subagent: bool = True

    # Outline
    outline_api_key: str = ""
    outline_base_url: str = "https://app.getoutline.com/api"

    # GitHub — comma-separated repos: owner/repo,owner/repo
    github_token: str = ""
    github_base_url: str = "https://api.github.com"
    github_repos: str = ""
    github_repo: str = ""  # fallback if github_repos empty
    github_repo_url: str = ""
    github_default_branch: str = "main"

    # Paths
    qa_agent_output_dir: Path = Path("./output")
    qa_agent_corpus_dir: Path = Path("./corpus")
    qa_agent_graphify_out: Path = Path("./graphify-out")

    # Domain profile — path to a JSON file that replaces the built-in
    # (search-pane example) domain vocabulary/contract rules. Empty = built-in.
    qa_agent_domain_rules: str = ""

    # Cost controls
    qa_agent_token_budget: int = 1500
    qa_agent_max_prs: int = Field(
        default=5,
        validation_alias=AliasChoices("QA_AGENT_MAX_PRS", "QA_AGENT_MAX_MRS"),
    )
    qa_agent_pr_scan_limit: int = 60
    qa_agent_max_prs_deep: int = 4
    qa_agent_max_docs: int = 5
    qa_agent_max_diff_lines: int = 200
    qa_agent_recursion_limit: int = 40

    @property
    def corpus_docs_dir(self) -> Path:
        return self.qa_agent_corpus_dir / "docs"

    @property
    def corpus_index_dir(self) -> Path:
        return self.qa_agent_corpus_dir / "index"

    @property
    def corpus_code_dir(self) -> Path:
        return self.qa_agent_corpus_dir / "code"

    @property
    def github_repos_list(self) -> list[str]:
        raw = self.github_repos or self.github_repo
        return [r.strip() for r in raw.split(",") if r.strip()]

    def github_repo_url_for(self, repo: str) -> str:
        """Build clone URL for owner/repo."""
        return f"https://github.com/{repo.strip()}.git"

    @property
    def cache_dir(self) -> Path:
        return Path(".cache")

    @staticmethod
    def model_profile_presets() -> dict[str, dict[str, str]]:
        """Preset model pairs: research (cheap/tool-heavy) + generate (quality)."""
        return {
            # Kimi research + GPT Gherkin — recommended
            "hybrid": {
                "research": "kimi:moonshot-v1-32k",
                "generate": "openai:gpt-4.1",
            },
            # All OpenAI — simplest setup
            "balanced": {
                "research": "openai:gpt-4.1-mini",
                "generate": "openai:gpt-4.1",
            },
            # All Kimi — maximum savings
            "budget": {
                "research": "kimi:moonshot-v1-32k",
                "generate": "kimi:moonshot-v1-32k",
            },
            "kimi": {
                "research": "openrouter:moonshotai/kimi-k2.7-code",
                "generate": "openrouter:moonshotai/kimi-k2.7-code",
            },
            # GLM — long-context repo analysis
            "glm": {
                "research": "glm:glm-5.2",
                "generate": "glm:glm-5.2",
            },
        }

    def uses_router(self) -> bool:
        """True when all LLM calls go through a single gateway."""
        return self.qa_agent_model_profile.lower() == "router"

    def role_base_url(self, role: str) -> str | None:
        # Nano rides the research endpoint; pro rides the generate endpoint.
        if role in ("research", "nano"):
            url = self.qa_agent_research_base_url
        else:
            url = self.qa_agent_generate_base_url
        return url or None

    def active_research_model(self) -> str:
        from qa_agent.models.llm import resolve_model_ref

        return resolve_model_ref(self, "research")

    def active_generate_model(self) -> str:
        from qa_agent.models.llm import resolve_model_ref

        return resolve_model_ref(self, "generate")


def get_settings() -> Settings:
    """Load settings from .env, then overlay UI preferences when present.

    Secrets and infra stay in ``.env``. Operational knobs (models, persistence,
    triage, research budgets) can be overridden from the Web Settings panel via
    ``.cache/ui_preferences.json``.
    """
    from qa_agent.preferences import apply_preferences

    return apply_preferences(Settings())
