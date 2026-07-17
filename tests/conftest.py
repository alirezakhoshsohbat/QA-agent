import os

# Keep the suite offline and deterministic: LLM-backed query expansion and
# triage are disabled via env (explicit Settings(...) kwargs in individual
# tests still override these).
os.environ.setdefault("QA_AGENT_QUERY_EXPANSION", "0")
os.environ.setdefault("QA_AGENT_TRIAGE", "0")
