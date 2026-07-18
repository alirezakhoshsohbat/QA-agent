from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware

from qa_agent.activity import (
    activity_from_tool_end,
    activity_from_tool_start,
    phase_for_tool,
)
from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker
from qa_agent.models.llm import (
    create_chat_model,
    create_research_model,
    resolve_model_ref,
)
from qa_agent.triage import TriageDecision, triage_request
from qa_agent.tools.github import create_github_tools
from qa_agent.tools.graphify import create_graphify_tools
from qa_agent.tools.outline import create_outline_tools
from qa_agent.tools.confluence import create_confluence_tools
from qa_agent.tools.azure_devops import create_azure_tools
from qa_agent.tools.openapi import create_openapi_tools
from qa_agent.tools.output import (
    MAX_WRITE_ATTEMPTS,
    create_output_tools,
    extract_gherkin_from_text,
    is_blocked_result,
)


def _load_prompt(name: str) -> str:
    path = Path(__file__).parent / "prompts" / name
    return path.read_text(encoding="utf-8")


def _domain_hint() -> str:
    """Domain vocabulary/contract hint from the active profile (data-driven, not
    baked into prompt text). Empty for the generic profile."""
    from qa_agent.domain import load_domain_rules

    rules = load_domain_rules()
    if not rules.prompt_hint:
        return ""
    return f"- Active domain profile: {rules.name}\n- {rules.prompt_hint}\n"


def _orchestrator_context(settings: Settings) -> str:
    repos = ", ".join(settings.github_repos_list) or "(none configured)"
    azure_repos = ", ".join(settings.azure_devops_repos_list) or "(none configured)"
    outline_sa = "enabled" if settings.qa_agent_outline_subagent else "DISABLED"
    github_sa = "enabled" if settings.qa_agent_github_subagent else "DISABLED"
    confluence_sa = "enabled" if settings.qa_agent_confluence_subagent else "DISABLED"
    azure_sa = "enabled" if settings.qa_agent_azure_subagent else "DISABLED"
    openapi_sa = "enabled" if settings.qa_agent_openapi_subagent else "DISABLED"
    return (
        "\n\n## Run configuration (injected)\n"
        f"- Configured GitHub repositories: {repos}\n"
        f"- Configured Azure DevOps repositories: {azure_repos}\n"
        f"- Confluence configured: {'yes' if settings.confluence_configured else 'no'}\n"
        f"- Azure DevOps configured: {'yes' if settings.azure_devops_configured else 'no'}\n"
        f"- OpenAPI configured: {'yes' if settings.openapi_configured else 'no'}\n"
        f"- Max documents to read: {settings.qa_agent_max_docs}\n"
        f"- Max PRs to deep-read: {settings.qa_agent_max_prs_deep}\n"
        f"- Graphify token budget: {settings.qa_agent_token_budget}\n"
        f"- Sub-agent outline-researcher: {outline_sa}\n"
        f"- Sub-agent github-researcher: {github_sa}\n"
        f"- Sub-agent confluence-researcher: {confluence_sa}\n"
        f"- Sub-agent azure-researcher: {azure_sa}\n"
        f"- Sub-agent openapi-researcher: {openapi_sa}\n"
        f"{_domain_hint()}"
    )


def _confluence_context(settings: Settings) -> str:
    return (
        "\n\n## Run configuration (injected)\n"
        f"- Read at most {settings.qa_agent_max_docs} pages total.\n"
        f"- Confluence {'is configured' if settings.confluence_configured else 'is NOT configured — rely on corpus fallback'}.\n"
        f"{_domain_hint()}"
    )


def _azure_context(settings: Settings) -> str:
    repos = settings.azure_devops_repos_list
    repos_str = ", ".join(repos) or "(none configured)"
    return (
        "\n\n## Run configuration (injected)\n"
        f"- Azure DevOps {'is configured' if settings.azure_devops_configured else 'is NOT configured'}.\n"
        f"- Configured repositories: {repos_str}\n"
        f"- Wiki: {settings.azure_devops_wiki or '(none configured)'}\n"
        f"- Deep-read up to {settings.qa_agent_max_prs_deep} PR(s) with azure_get_pr_changes.\n"
        "- Research work items (Boards) and wiki for acceptance criteria first, then PRs for code.\n"
        f"{_domain_hint()}"
    )


def _openapi_context(settings: Settings) -> str:
    specs = settings.openapi_specs_list
    specs_str = f"{len(specs)} spec(s) configured" if specs else "(none configured)"
    return (
        "\n\n## Run configuration (injected)\n"
        f"- OpenAPI {'is configured' if settings.openapi_configured else 'is NOT configured'}.\n"
        f"- Sources: {specs_str}\n"
        f"- Read at most {settings.qa_agent_max_docs} endpoints total.\n"
        "- Extract exact wire facts (method, path, auth, params, request/response "
        "schema, status codes) and derive validation/auth/boundary edge cases.\n"
        f"{_domain_hint()}"
    )


def _outline_context(settings: Settings) -> str:
    return (
        "\n\n## Run configuration (injected)\n"
        f"- Read at most {settings.qa_agent_max_docs} documents total.\n"
        f"- Outline API {'is configured' if settings.outline_api_key else 'is NOT configured — rely on corpus_search_docs'}.\n"
        f"{_domain_hint()}"
    )


def _github_context(settings: Settings) -> str:
    repos = settings.github_repos_list
    repos_str = ", ".join(repos) or "(none configured)"
    line = f"- Configured repositories: {repos_str}\n"
    if not repos:
        line += "- No repositories configured — report doc-only coverage and stop.\n"
    return (
        "\n\n## Run configuration (injected)\n"
        f"{line}"
        f"- Deep-read up to {settings.qa_agent_max_prs_deep} PR(s) with full diffs "
        f"(github_get_pr_changes on each).\n"
        f"- Discovery scans ~{settings.qa_agent_pr_scan_limit} recent PRs across repos.\n"
        "- Always pass an explicit `repo=` on every GitHub tool call.\n"
        "- PR diff research is **mandatory** when repos are configured — do not stop at titles.\n"
        f"{_domain_hint()}"
    )


def _extract_token_usage(result: dict) -> tuple[int, int]:
    """Best-effort extraction of token usage from agent result.

    Prefers real provider usage metadata. Some OpenAI-compatible endpoints omit
    usage on streamed responses even with ``stream_usage``; when no usage is
    reported at all we fall back to a rough char-based estimate so the cost
    widget reflects that work happened instead of showing a misleading $0.00.
    """
    input_tokens = 0
    output_tokens = 0
    found_usage = False

    messages = result.get("messages", [])
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None) or getattr(msg, "response_metadata", {}).get(
            "token_usage", {}
        )
        if usage:
            found_usage = True
            if isinstance(usage, dict):
                input_tokens += usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                output_tokens += usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
            else:
                input_tokens += getattr(usage, "input_tokens", 0)
                output_tokens += getattr(usage, "output_tokens", 0)

    if not found_usage and messages:
        input_tokens, output_tokens = _estimate_token_usage(messages)

    return input_tokens, output_tokens


def _message_text_length(msg: Any) -> int:
    """Approximate character length of a message's textual content."""
    content = getattr(msg, "content", msg if isinstance(msg, str) else "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                total += len(str(part.get("text", "")))
        return total
    return len(str(content or ""))


def _estimate_token_usage(messages: list[Any]) -> tuple[int, int]:
    """Rough ~4 chars/token fallback when providers omit usage metadata."""
    input_chars = 0
    output_chars = 0
    for msg in messages:
        msg_type = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
        length = _message_text_length(msg)
        if msg_type in {"ai", "assistant"}:
            output_chars += length
        else:
            input_chars += length
    return input_chars // 4, output_chars // 4


def create_qa_agent(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
    request_query: str | None = None,
    generate_role: str = "generate",
):
    settings = settings or get_settings()
    cost = cost or CostTracker(budget=settings.qa_agent_token_budget)

    outline_tools = create_outline_tools(settings, cost)
    outline_research_tools = create_outline_tools(settings, cost, research_only=True)
    github_tools = create_github_tools(settings, cost)
    confluence_tools = create_confluence_tools(settings, cost)
    azure_tools = create_azure_tools(settings, cost)
    openapi_tools = create_openapi_tools(settings, cost)
    graphify_tools = create_graphify_tools(settings, cost)
    output_tools = create_output_tools(
        settings.qa_agent_output_dir,
        cost,
        request_query=request_query,
        max_write_attempts=settings.qa_agent_max_write_attempts,
    )

    orchestrator_prompt = _load_prompt("orchestrator.md") + _orchestrator_context(settings)
    outline_prompt = _load_prompt("outline_researcher.md") + _outline_context(settings)
    github_prompt = _load_prompt("github_researcher.md") + _github_context(settings)
    confluence_prompt = _load_prompt("confluence_researcher.md") + _confluence_context(settings)
    azure_prompt = _load_prompt("azure_researcher.md") + _azure_context(settings)
    openapi_prompt = _load_prompt("openapi_researcher.md") + _openapi_context(settings)

    research_model = create_research_model(settings)
    generate_model = create_chat_model(
        resolve_model_ref(settings, generate_role), settings, role=generate_role
    )

    outline_only_excluded = frozenset({
        "grep",
        "glob",
        "ls",
        "write_file",
        "edit_file",
        "execute",
    })

    subagents: list[dict[str, Any]] = []
    if settings.qa_agent_outline_subagent:
        subagents.append(
            {
                "name": "outline-researcher",
                "description": (
                    "Researches the documentation for a feature: runs outline_research_bundle, "
                    "reads the top few relevant docs, and returns an acceptance checklist plus "
                    "wire facts for QA. Use first, for any feature."
                ),
                "system_prompt": outline_prompt,
                "tools": outline_research_tools,
                "middleware": [_ToolExclusionMiddleware(excluded=outline_only_excluded)],
                "model": research_model,
            }
        )
    if settings.qa_agent_github_subagent:
        subagents.append(
            {
                "name": "github-researcher",
                "description": (
                    "Git/PR code research: discovers related pull requests across configured "
                    "repos, reads diffs for every plausible match (up to the deep-read limit), "
                    "and returns wire-level test implications. Always delegate when repos exist."
                ),
                "system_prompt": github_prompt,
                "tools": github_tools,
                "model": research_model,
            }
        )
    if settings.qa_agent_confluence_subagent and settings.confluence_configured:
        subagents.append(
            {
                "name": "confluence-researcher",
                "description": (
                    "Researches Confluence documentation for a feature: runs "
                    "confluence_research_bundle, reads the top relevant pages, and returns an "
                    "acceptance checklist plus wire facts. Use when Confluence is configured."
                ),
                "system_prompt": confluence_prompt,
                "tools": confluence_tools,
                "model": research_model,
            }
        )
    if settings.qa_agent_azure_subagent and settings.azure_devops_configured:
        subagents.append(
            {
                "name": "azure-researcher",
                "description": (
                    "Azure DevOps research: reads Boards work items and Wiki for requirements/"
                    "acceptance criteria, discovers pull requests and reads their changed files "
                    "for wire-level code evidence. Use when Azure DevOps is configured."
                ),
                "system_prompt": azure_prompt,
                "tools": azure_tools,
                "model": research_model,
            }
        )
    if settings.qa_agent_openapi_subagent and settings.openapi_configured:
        subagents.append(
            {
                "name": "openapi-researcher",
                "description": (
                    "OpenAPI/Swagger contract research: discovers endpoints for a feature, "
                    "reads their full request/response contracts, and returns wire-level facts "
                    "(auth, params, schemas, status codes) plus API edge cases. Use when "
                    "OpenAPI is configured, especially for API/backend test generation."
                ),
                "system_prompt": openapi_prompt,
                "tools": openapi_tools,
                "model": research_model,
            }
        )

    all_tools = (
        outline_tools
        + github_tools
        + confluence_tools
        + azure_tools
        + openapi_tools
        + graphify_tools
        + output_tools
    )

    agent = create_deep_agent(
        model=generate_model,
        tools=all_tools,
        system_prompt=orchestrator_prompt,
        subagents=subagents,
    )

    return agent, cost


def _agent_config(settings: Settings) -> dict[str, Any]:
    return {"recursion_limit": settings.qa_agent_recursion_limit}


def _build_prompt(query: str, effective_budget: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    steps: list[str] = []
    step_n = 1
    if settings.qa_agent_outline_subagent:
        steps.append(
            f"{step_n}. Delegate to **outline-researcher** first to obtain the acceptance "
            "checklist and wire facts for this feature."
        )
        step_n += 1
    else:
        steps.append(
            f"{step_n}. Research docs yourself with Outline/corpus tools (outline-researcher "
            "is disabled) and build the acceptance checklist."
        )
        step_n += 1
    if settings.qa_agent_github_subagent:
        steps.append(
            f"{step_n}. Delegate to **github-researcher** when repos are configured (short "
            "feature name). It must read PR **diffs** and return wire-level findings — "
            "not titles only."
        )
        step_n += 1
    elif settings.github_repos_list:
        steps.append(
            f"{step_n}. Use GitHub tools directly (github-researcher is disabled) to read "
            "related PR diffs when repos are configured."
        )
        step_n += 1
    if settings.qa_agent_confluence_subagent and settings.confluence_configured:
        steps.append(
            f"{step_n}. Delegate to **confluence-researcher** to mine Confluence pages for "
            "acceptance criteria and wire facts."
        )
        step_n += 1
    if settings.qa_agent_azure_subagent and settings.azure_devops_configured:
        steps.append(
            f"{step_n}. Delegate to **azure-researcher** to gather Boards/Wiki requirements and "
            "read Azure DevOps PR changes for wire-level code evidence."
        )
        step_n += 1
    if settings.qa_agent_openapi_subagent and settings.openapi_configured:
        steps.append(
            f"{step_n}. Delegate to **openapi-researcher** to read the API contract "
            "(endpoints, params, request/response schemas, status codes) and derive "
            "API validation/auth/boundary edge cases."
        )
        step_n += 1
    steps.append(
        f"{step_n}. Write Gherkin from docs **and** PR findings. Cover **every** checklist "
        "ID (E-1, B-8, … verbatim — never rename to AC-*) with `@acceptance-{ID}` tags, then "
        "**expand each behavior in depth**: every high-risk criterion gets MULTIPLE scenarios "
        "(happy + edge + failure + retry + rehydration + race + cross-tab + regression as "
        "applicable), reusing its `@acceptance-{ID}`. One scenario per ID is NOT enough — aim "
        "for several scenarios per non-trivial ID. Write the narrative (titles, Given/When/Then "
        "prose, comments) in the SAME language as the REQUEST above; keep Gherkin keywords, "
        "tags, endpoints, storage keys, JSON, HTTP verbs, and status codes in English. Include "
        "the header legend, priority/type tags, and Background required by your system prompt."
    )
    step_n += 1
    steps.append(
        f"{step_n}. Call `write_feature_file` once with the complete Gherkin, `sources_json`, "
        "and `acceptance_checklist_json` (every PRD row, IDs verbatim). Fix any BLOCKED "
        "errors and retry with the next `write_attempt` if needed."
    )

    return f"""Generate Gherkin test cases for the following QA request.

REQUEST:
{query}

Follow your system instructions. In short:
{chr(10).join(steps)}

Aim for full PRD table coverage (e.g. all E-* frontend rows + all B-* BFF rows),
not a small merged subset.

Do not loop on research: each tool call must add new information, and you must
proceed to writing as soon as the criteria are testable. Optional graphify
queries share a token budget of {effective_budget}.
"""


ActivityCallback = Callable[[dict[str, Any]], None]


# Fallback ceiling on total re-prompts when no per-run limits are supplied.
MAX_TOTAL_REPROMPTS = 8


@dataclass(frozen=True)
class RunLimits:
    """Bounded persistence budget for one run (per model tier).

    The agent keeps deciding and self-correcting until it lands a clean,
    fully-covered feature file or exhausts these ceilings — bounded so a
    genuinely impossible ask cannot loop forever or burn unlimited tokens.
    """

    max_write_attempts: int = MAX_WRITE_ATTEMPTS
    max_reprompts: int = MAX_TOTAL_REPROMPTS
    quality_refine_rounds: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> "RunLimits":
        return cls(
            max_write_attempts=max(1, settings.qa_agent_max_write_attempts),
            max_reprompts=max(1, settings.qa_agent_max_reprompts),
            quality_refine_rounds=max(0, settings.qa_agent_quality_refine_rounds),
        )


def _has_quality_warnings(write_output: str | None) -> bool:
    """True when the last successful write reported non-blocking quality warnings."""
    return bool(write_output) and "Quality warnings (" in write_output


def _blocked_retry_prompt(blocked_output: str, next_attempt: int) -> str:
    return (
        "write_feature_file was BLOCKED. Do NOT ask the user for confirmation — "
        "you are autonomous and must keep working until the file is written. Fix "
        "EVERY listed error yourself (add missing @acceptance-{ID}/@wip-{ID} tags, "
        "move orphan scenarios inside a Feature, use only checklist IDs, fix syntax) "
        f"and call write_feature_file again NOW with write_attempt={next_attempt} and "
        "the complete corrected Gherkin:\n\n"
        f"{blocked_output}"
    )


def _stall_nudge_prompt() -> str:
    return (
        "You stopped without saving the feature file. You are autonomous and must "
        "not ask questions or wait for confirmation. Call write_feature_file NOW "
        "with the complete Gherkin, sources_json, and acceptance_checklist_json. "
        "Every Scenario must sit inside a Feature and carry an @acceptance-{ID} or "
        "@wip-{ID} tag that exists in the checklist you pass."
    )


def _quality_refine_prompt(write_output: str) -> str:
    return (
        "The feature file was saved but validation flagged QUALITY WARNINGS. You are "
        "autonomous — do not stop at a mediocre result. Revise the flagged scenarios "
        "so every step is concrete: replace vague/abstract wording with real endpoints, "
        "field names, status codes, and sample payloads from the research findings; "
        "remove duplicate-intent scenarios. Keep every checklist ID covered, then call "
        "write_feature_file again with the improved Gherkin. Warnings:\n\n"
        f"{write_output}"
    )


def _last_write_feature_tool_output(result: dict) -> str | None:
    for message in reversed(result.get("messages", [])):
        name = getattr(message, "name", None)
        if name == "write_feature_file":
            return str(getattr(message, "content", ""))
    return None


def _count_write_feature_calls(result: dict) -> int:
    return sum(
        1
        for message in result.get("messages", [])
        if getattr(message, "name", None) == "write_feature_file"
    )


@dataclass
class _RoundState:
    write_attempts: int = 0
    reprompts: int = 0
    refine_used: int = 0
    prev_write_count: int = 0


def _plan_next_round(
    result: dict,
    state: _RoundState,
    limits: RunLimits,
) -> str | None:
    """Decide the follow-up prompt (if any) after an agent round.

    Mutates ``state`` and returns the next prompt, or None to stop the loop.
    "Done" means a clean write landed AND (when refinement is enabled) its
    remaining quality-warning budget is spent.
    """
    write_count = _count_write_feature_calls(result)
    fresh_write = write_count > state.prev_write_count
    state.prev_write_count = write_count
    last_output = _last_write_feature_tool_output(result)
    blocked = bool(last_output and is_blocked_result(last_output))

    # A clean, validated write landed.
    if write_count > 0 and not blocked:
        # Bounded quality refinement so "done" means concrete & covered, not just
        # structurally valid.
        if (
            limits.quality_refine_rounds > 0
            and state.refine_used < limits.quality_refine_rounds
            and state.reprompts < limits.max_reprompts
            and _has_quality_warnings(last_output)
        ):
            state.refine_used += 1
            state.reprompts += 1
            return _quality_refine_prompt(last_output or "")
        return None

    if blocked and fresh_write:
        state.write_attempts += 1

    # Out of budget — stop and let the caller escalate or report.
    if state.write_attempts >= limits.max_write_attempts or state.reprompts >= limits.max_reprompts:
        return None

    state.reprompts += 1
    if blocked:
        return _blocked_retry_prompt(last_output or "", state.write_attempts + 1)
    return _stall_nudge_prompt()


async def _run_agent_streaming(
    agent,
    messages: list[Any],
    on_activity: ActivityCallback | None = None,
    config: dict[str, Any] | None = None,
) -> dict:
    """Run agent with live tool-call activity events."""
    pending_inputs: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"messages": []}
    run_config = config or {}

    async for event in agent.astream_events(
        {"messages": messages},
        config=run_config,
        version="v2",
    ):
        kind = event.get("event")
        if kind == "on_tool_start":
            tool = event.get("name", "")
            run_id = event.get("run_id", "")
            tool_input = event.get("data", {}).get("input") or {}
            if run_id:
                pending_inputs[run_id] = tool_input

            activity = activity_from_tool_start(tool, tool_input)
            activity["phase"] = phase_for_tool(tool)
            if on_activity:
                on_activity(activity)

            if tool == "write_feature_file":
                raw = tool_input.get("gherkin_content") or ""
                gherkin = extract_gherkin_from_text(raw)
                if gherkin and on_activity:
                    on_activity({"type": "partial_gherkin", "gherkin": gherkin})

        elif kind == "on_tool_end":
            tool = event.get("name", "")
            run_id = event.get("run_id", "")
            tool_input = pending_inputs.pop(run_id, {}) if run_id else {}
            output = event.get("data", {}).get("output")
            activity = activity_from_tool_end(tool, tool_input, output)
            activity["phase"] = phase_for_tool(tool)
            if on_activity:
                on_activity(activity)

        elif kind == "on_chain_end":
            output = event.get("data", {}).get("output") or {}
            # Capture any graph/chain end that carries a message list. The final
            # top-level graph end arrives last, so the last capture wins. This is
            # robust to the compiled graph's name changing across versions.
            if isinstance(output, dict) and output.get("messages"):
                result = output

    return result


def _final_write_blocked(result: dict) -> bool:
    """A run is 'blocked' only if no clean feature write ever landed."""
    write_count = _count_write_feature_calls(result)
    if write_count == 0:
        return True
    last_output = _last_write_feature_tool_output(result)
    return bool(last_output and is_blocked_result(last_output))


async def _run_agent_streaming_with_retry(
    agent,
    prompt: str,
    on_activity: ActivityCallback | None = None,
    config: dict[str, Any] | None = None,
    limits: RunLimits | None = None,
) -> tuple[dict, int, bool]:
    limits = limits or RunLimits()
    state = _RoundState()
    messages: list[Any] = [{"role": "user", "content": prompt}]
    result: dict[str, Any] = {"messages": []}

    while True:
        result = await _run_agent_streaming(agent, messages, on_activity, config)
        next_prompt = _plan_next_round(result, state, limits)
        if next_prompt is None:
            break
        messages = [*result["messages"], {"role": "user", "content": next_prompt}]

    return result, state.write_attempts, _final_write_blocked(result)


def _run_agent_with_retry(
    agent,
    prompt: str,
    config: dict[str, Any] | None = None,
    limits: RunLimits | None = None,
) -> tuple[dict, int, bool]:
    limits = limits or RunLimits()
    state = _RoundState()
    messages: list[Any] = [{"role": "user", "content": prompt}]
    result: dict[str, Any] = {"messages": []}

    while True:
        result = agent.invoke({"messages": messages}, config=config or {})
        next_prompt = _plan_next_round(result, state, limits)
        if next_prompt is None:
            break
        messages = [*result["messages"], {"role": "user", "content": next_prompt}]

    return result, state.write_attempts, _final_write_blocked(result)


def _escalation_prompt(
    query: str,
    effective_budget: int,
    blocked_output: str | None,
    settings: Settings | None = None,
) -> str:
    note = (
        "\n\nIMPORTANT: A previous attempt with a smaller model failed validation. "
        "You are the stronger model brought in to fix this. "
    )
    if blocked_output:
        note += f"The last write_feature_file attempt was BLOCKED with:\n\n{blocked_output}\n\n"
    note += (
        "Resolve every validation error (tags, checklist IDs, Feature structure, "
        "syntax) and produce the complete corrected Gherkin."
    )
    return _build_prompt(query, effective_budget, settings) + note


def _execute_run(
    agent,
    prompt: str,
    on_activity: ActivityCallback | None,
    run_config: dict[str, Any],
    limits: RunLimits,
) -> tuple[dict, int, bool]:
    if on_activity:
        return asyncio.run(
            _run_agent_streaming_with_retry(agent, prompt, on_activity, run_config, limits)
        )
    return _run_agent_with_retry(agent, prompt, run_config, limits)


def _can_escalate(settings: Settings, current_role: str) -> bool:
    """Escalation only makes sense when pro resolves to a different model."""
    if not settings.qa_agent_escalation or current_role == "pro":
        return False
    try:
        return resolve_model_ref(settings, "pro") != resolve_model_ref(settings, current_role)
    except ValueError:
        return False


def run_generate(
    query: str,
    settings: Settings | None = None,
    budget: int | None = None,
    on_activity: ActivityCallback | None = None,
    request_query: str | None = None,
) -> dict:
    settings = settings or get_settings()
    effective_budget = budget or settings.qa_agent_token_budget
    cost = CostTracker(budget=effective_budget)

    # Complexity triage picks the generate tier upfront (pro for complex asks).
    decision: TriageDecision = triage_request(query, settings)
    generate_role = decision.generate_role
    try:
        resolve_model_ref(settings, generate_role)
    except ValueError:
        generate_role = "generate"
    cost.record_triage(decision.complexity, decision.source, generate_role)

    if on_activity:
        on_activity(
            {
                "type": "triage",
                "phase": "triage",
                "complexity": decision.complexity,
                "reason": decision.reason,
                "generate_role": generate_role,
                "source": decision.source,
            }
        )

    # request_query controls the output filename/metadata; the prompt uses `query`.
    agent, cost = create_qa_agent(
        settings, cost, request_query=request_query or query, generate_role=generate_role
    )
    prompt = _build_prompt(query, effective_budget, settings)
    run_config = _agent_config(settings)
    limits = RunLimits.from_settings(settings)

    result, write_attempts, write_blocked = _execute_run(
        agent, prompt, on_activity, run_config, limits
    )

    escalated = False
    if write_blocked and _can_escalate(settings, generate_role):
        blocked_output = _last_write_feature_tool_output(result)
        if on_activity:
            on_activity(
                {
                    "type": "escalation",
                    "phase": "generate",
                    "detail": "نوشتن بلاک شد — تلاش دوباره با مدل حرفه‌ای",
                }
            )
        pro_agent, cost = create_qa_agent(
            settings, cost, request_query=request_query or query, generate_role="pro"
        )
        pro_prompt = _escalation_prompt(query, effective_budget, blocked_output, settings)
        pro_result, pro_attempts, pro_blocked = _execute_run(
            pro_agent, pro_prompt, on_activity, run_config, limits
        )
        # Keep the escalated run's outcome; token usage of both runs is summed
        # below via the second result's messages plus the first run's counts.
        first_input, first_output = _extract_token_usage(result)
        cost.record_llm_usage(first_input, first_output)
        result = pro_result
        write_attempts += pro_attempts
        write_blocked = pro_blocked
        generate_role = "pro"
        escalated = True
        cost.record_escalation()

    input_tokens, output_tokens = _extract_token_usage(result)
    cost.record_llm_usage(input_tokens, output_tokens)
    cost.estimate_usd(resolve_model_ref(settings, generate_role))
    cost.append_to_global(settings.qa_agent_graphify_out)

    messages = result.get("messages") or []
    if messages:
        final_message = messages[-1]
        content = getattr(final_message, "content", str(final_message))
    else:
        content = ""
    blocked_output = _last_write_feature_tool_output(result)

    return {
        "result": result,
        "response": content,
        "cost": cost.report,
        "write_blocked": write_blocked,
        "write_attempt": write_attempts,
        "blocked_output": blocked_output if write_blocked else None,
        "triage": {
            "complexity": decision.complexity,
            "reason": decision.reason,
            "source": decision.source,
            "generate_role": generate_role,
        },
        "escalated": escalated,
    }


def build_pr_query(repo: str, number: int, settings: Settings | None = None) -> tuple[str, str]:
    """Fetch a PR and build (rich_prompt_query, clean_display_query) for diff-driven runs."""
    from qa_agent.tools.github import GitHubClient

    settings = settings or get_settings()
    client = GitHubClient(settings)
    pr = client.get_pull_request(number, repo)
    files = client.get_pr_files(number, repo)

    title = pr.get("title", "") or f"PR #{number}"
    body = str(pr.get("body", "") or "")[:1500]
    paths = [f.get("filename", "") for f in files if f.get("filename")]
    file_list = "\n".join(f"- {p}" for p in paths[:40])

    rich_query = (
        f"Generate QA test cases for the behavior changed by pull request "
        f"#{number} in repository {repo}.\n\n"
        f"PR title: {title}\n\n"
        f"PR description:\n{body or '(no description)'}\n\n"
        f"Changed files ({len(paths)}):\n{file_list}\n\n"
        "Delegate to github-researcher to read this PR's diff "
        f"(github_get_pr_changes with repo=\"{repo}\" and number={number}), and "
        "cross-reference Outline docs for acceptance criteria. Focus tests on the "
        "behavior this PR adds or changes, tag scenarios with @pr-"
        f"{number}, and cover documented acceptance criteria where they apply."
    )
    clean_query = f"PR {repo}#{number}: {title}"
    return rich_query, clean_query


def run_generate_from_pr(
    repo: str,
    number: int,
    settings: Settings | None = None,
    budget: int | None = None,
    on_activity: ActivityCallback | None = None,
) -> dict:
    settings = settings or get_settings()
    rich_query, clean_query = build_pr_query(repo, number, settings)
    return run_generate(
        rich_query,
        settings=settings,
        budget=budget,
        on_activity=on_activity,
        request_query=clean_query,
    )
