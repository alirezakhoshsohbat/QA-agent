# QA Agent

Generate high-quality, executable **Gherkin** test cases for **any** feature by
researching Outline documentation and GitHub pull requests. Built on Deep Agents
with a research/generation model split and graphify for cost-efficient context
retrieval.

Requests can be written in Persian or English; the generated test cases are
always English Gherkin.

## Features

- **Feature-agnostic**: works for any feature/QA request, not a fixed domain.
- **Multi-agent research**: an orchestrator delegates to an `outline-researcher`
  (docs → acceptance checklist + wire facts) and a `github-researcher`
  (PR impact analysis) — dispatched in parallel — before writing.
- **Smart retrieval**: LLM query understanding (Persian → English search terms
  via the nano tier) + hybrid BM25/embedding corpus search fused with RRF, plus
  a one-page layer-grouped corpus map researchers read before searching.
- **Complexity-tiered models**: a triage step routes complex requests to the
  pro model tier upfront, and a run whose feature write stays BLOCKED
  auto-escalates to the pro model for one corrected attempt. Tier decisions are
  recorded in `.cost.json`.
- **Enforced coverage contract**: every acceptance criterion is mapped to a
  `@acceptance-{ID}` scenario; `write_feature_file` validates structure,
  coverage, and quality before saving, and the agent auto-fixes and retries.
- **Diff-driven mode**: generate test cases directly from a specific PR.
- **Traceability matrix**: export acceptance ID → scenario → source as CSV/HTML.
- **Coverage-gap report**: compare generated scenarios against existing repo
  tests (`.feature`, JS/TS `it/test/describe`, pytest).
- **Quality-gate eval suite**: offline fixtures for CI regression, plus a live
  mode.
- **Web UI + CLI**, token-budget controls, and per-run cost tracking.
- Local `.feature` output with `.meta.json` and `.cost.json` sidecars.

## Setup

```bash
cd "QA agent"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[graphify,dev]"
cp .env.example .env
# Edit .env with your API keys
```

## Usage

### Index corpus (Outline docs + GitHub code)

```bash
qa-agent index --outline-sync --github-clone
# add --with-graph to build the graphify knowledge graph (slow)
```

### Generate Gherkin test cases

```bash
qa-agent generate "راجب احراز هویت و لاگین کاربر تست کیس بساز" --budget 1500
```

### Generate from a specific pull request (diff-driven)

```bash
qa-agent from-pr chimney-ai/web 1234
```

### Build a traceability matrix

```bash
qa-agent trace output/<name>.feature --format both
# writes <name>.trace.csv and <name>.trace.html next to the feature file
```

### Report coverage gaps vs existing tests

```bash
qa-agent coverage-gap output/<name>.feature --against corpus/code
qa-agent coverage-gap output/<name>.feature --json
```

### Run the quality-gate eval suite

```bash
qa-agent eval                 # offline: score fixtures in evals/cases.json
qa-agent eval --live          # also run cases that define a live query
```

### Start the web UI

```bash
qa-agent serve --host 127.0.0.1 --port 8787
```

### Inspect model configuration

```bash
qa-agent models
```

## Output files

Written to `./output/`:

- `{slug}-{timestamp}.feature` — Gherkin scenarios
- `{slug}-{timestamp}.meta.json` — sources, acceptance checklist, coverage,
  validation errors, quality warnings
- `{slug}-{timestamp}.cost.json` — token and API usage
- `{slug}-{timestamp}.trace.csv` / `.trace.html` — traceability matrix (via `trace`)

## Architecture

```
User request (Persian/English)
        │
        ▼
Triage (nano model, heuristic fallback) → simple/standard → generate model
        │                                 complex          → pro model
        ▼
Orchestrator (chosen tier)
  ├── outline-researcher (research model) ┐ dispatched in parallel
  │     corpus_map → search → read docs   │ → acceptance checklist + wire facts
  ├── github-researcher  (research model) ┘ → PR triage + test implications
  │        └── graphify_query (optional, budget-capped)
  └── write_feature_file → validate (structure + coverage + quality)
             │  BLOCKED? auto-fix and retry (max 2 attempts)
             │  still BLOCKED? escalate once to the pro model
             ▼
        ./output/*.feature  (+ .meta.json, .cost.json)

Retrieval: nano-model query expansion (cached) → Outline search + hybrid
BM25/embedding corpus search (RRF fusion), indexed by `qa-agent index`.
```

Post-generation analysis (deterministic, no LLM): `trace`, `coverage-gap`, `eval`.

## Model configuration (router mode — default)

One gateway, one token, a different model name per role:

```env
QA_AGENT_MODEL_PROFILE=router
LLM_API_KEY=sk-...
LLM_BASE_URL=https://your-router.example/v1

QA_AGENT_RESEARCH_MODEL=moonshotai/kimi-k2.7-code
QA_AGENT_GENERATE_MODEL=openai/gpt-4.1

# Optional tiers
QA_AGENT_NANO_MODEL=openai/gpt-4.1-mini   # query expansion + triage (default: research model)
QA_AGENT_PRO_MODEL=openai/gpt-5           # complex requests + escalation (default: off)
```

Other profiles (`hybrid`, `balanced`, `budget`, `glm`, `custom`) are available
when `QA_AGENT_MODEL_PROFILE` is not `router`.

### Smart retrieval & tier routing

- `QA_AGENT_QUERY_EXPANSION=1` — nano model rewrites Persian/vague requests
  into English search terms (cached in `.cache/query_expansion/`; heuristic
  fallback when the model is unavailable).
- `QA_AGENT_EMBEDDING_MODEL=text-embedding-3-small` — adds vector search over
  the local corpus on top of BM25 (hybrid, RRF-fused). Endpoint/key default to
  `LLM_BASE_URL`/`LLM_API_KEY`. Leave empty for BM25-only.
- `QA_AGENT_TRIAGE=1` — classify request complexity before generation; complex
  requests use `QA_AGENT_PRO_MODEL`.
- `QA_AGENT_ESCALATION=1` — when the feature write is still BLOCKED after
  retries, rerun once with the pro model.
- `qa-agent index` also builds the BM25/embedding index and the corpus map
  (`corpus/index/`).

## Outline & GitHub configuration

```env
OUTLINE_API_KEY=ol_api_...
OUTLINE_BASE_URL=https://your-team.getoutline.com/api

GITHUB_TOKEN=ghp_...
GITHUB_REPOS=owner/web,owner/bot      # comma-separated; multiple repos supported
GITHUB_DEFAULT_BRANCH=main
```

Create a GitHub token at Settings → Developer settings → Personal access tokens
with `repo` scope. Configured repos are injected into the research prompts at
run time, so no repo names are hard-coded.

## Domain profile (feature-agnostic)

All domain-specific vocabulary lives in **data**, not code: the concrete-signal
patterns, forbidden wire shapes, Outline search aliases, doc-ID hints, PR path
markers, and the prompt vocabulary hint are loaded from a domain profile at
runtime (`src/qa_agent/domain.py`).

- **Default**: a built-in "search-pane" example profile (keeps existing
  behavior).
- **Override**: point `QA_AGENT_DOMAIN_RULES` at a JSON file, or drop a
  `domain_rules.json` in the working directory. Copy
  [`domain_rules.example.json`](domain_rules.example.json) and edit it for your
  product.
- **Fully generic**: set the profile to a minimal `{}` file — the agent then
  relies only on generic structural heuristics (endpoints, JSON literals, HTTP
  status codes) and no product-specific rules.

```env
QA_AGENT_DOMAIN_RULES=./domain_rules.json
```

Profile keys: `name`, `prompt_hint`, `concrete_signals`, `domain_context_terms`,
`forbidden_patterns`, `vague_phrases`, `outline_search_aliases`,
`outline_doc_id_hints`, `layer_hints`, `pr_search_aliases`, `pr_path_markers`,
`pr_query_focus_patterns`, `pr_false_positives`. Any omitted key defaults to
empty. Regex case-insensitivity is written inline as `(?i)`.

## Environment variables

See [`.env.example`](.env.example) for all configuration options.

## Cost controls

- `--budget` caps graphify retrieval tokens per run.
- `QA_AGENT_MAX_PRS` / `QA_AGENT_MAX_PRS_DEEP` / `QA_AGENT_MAX_DOCS` bound the
  research surface; `QA_AGENT_MAX_DIFF_LINES` auto-summarizes large diffs.
- Research subagents use the cheaper model tier; the generate model is reserved
  for writing Gherkin.

## Development

```bash
pytest -q          # full unit + reporting + offline eval suite
qa-agent eval      # quality-gate regression on golden fixtures
```
