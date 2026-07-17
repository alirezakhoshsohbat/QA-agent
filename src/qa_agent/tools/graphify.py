from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker


def _graphify_available() -> bool:
    return shutil.which("graphify") is not None


def _run_graphify_cli(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        ["graphify", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    return result.returncode, result.stdout, result.stderr


def graph_exists(graphify_out: Path) -> bool:
    return (graphify_out / "graph.json").exists()


def build_graph(corpus_dir: Path, graphify_out: Path, update: bool = True) -> Path:
    """Build or update graphify knowledge graph from corpus."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    graphify_out.mkdir(parents=True, exist_ok=True)

    # Empty corpus — write placeholder graph
    has_files = any(corpus_dir.rglob("*")) if corpus_dir.exists() else False
    if not has_files:
        placeholder = {
            "nodes": [],
            "edges": [],
            "note": "Empty corpus. Run: qa-agent index --outline-sync --github-clone",
        }
        (graphify_out / "graph.json").write_text(
            json.dumps(placeholder, indent=2), encoding="utf-8"
        )
        return graphify_out

    if not _graphify_available():
        placeholder = {
            "nodes": [],
            "edges": [],
            "note": "graphify CLI not installed; run: pip install graphifyy",
        }
        (graphify_out / "graph.json").write_text(
            json.dumps(placeholder, indent=2), encoding="utf-8"
        )
        return graphify_out

    args = [str(corpus_dir)]
    if update:
        args.append("--update")

    code, stdout, stderr = _run_graphify_cli(args, cwd=corpus_dir.parent)
    if code != 0 and not graph_exists(graphify_out):
        # Graceful fallback on empty extraction
        placeholder = {
            "nodes": [],
            "edges": [],
            "note": f"graphify build skipped: {(stderr or stdout)[:300]}",
        }
        (graphify_out / "graph.json").write_text(
            json.dumps(placeholder, indent=2), encoding="utf-8"
        )
        return graphify_out

    src_graph = corpus_dir.parent / "graphify-out" / "graph.json"
    if src_graph.exists() and src_graph != graphify_out / "graph.json":
        graphify_out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_graph, graphify_out / "graph.json")

    return graphify_out


def query_graph(
    question: str,
    graphify_out: Path,
    budget: int = 1500,
    cost: CostTracker | None = None,
) -> str:
    """Query the knowledge graph with a token budget."""
    if not graph_exists(graphify_out):
        return (
            "Knowledge graph not built yet. Run: qa-agent index --outline-sync --github-clone"
        )

    if not _graphify_available():
        return _fallback_graph_query(graphify_out, question, budget)

    remaining = budget
    if cost:
        remaining = cost.graphify_budget_remaining()

    args = ["query", question, "--budget", str(remaining)]
    code, stdout, stderr = _run_graphify_cli(args, cwd=graphify_out.parent)
    output = stdout.strip() or stderr.strip() or "No graph results."

    if cost:
        cost.record_graphify_tokens(min(len(output.split()), remaining))

    return output


def path_graph(
    source: str,
    target: str,
    graphify_out: Path,
    cost: CostTracker | None = None,
) -> str:
    """Find shortest path between two concepts in the knowledge graph."""
    if not graph_exists(graphify_out):
        return "Knowledge graph not built yet."

    if not _graphify_available():
        return f"Path query unavailable (graphify not installed): {source} → {target}"

    args = ["path", source, target]
    code, stdout, stderr = _run_graphify_cli(args, cwd=graphify_out.parent)
    output = stdout.strip() or stderr.strip() or "No path found."
    if cost:
        cost.record_graphify_tokens(min(len(output.split()), 500))
    return output


def _fallback_graph_query(graphify_out: Path, question: str, budget: int) -> str:
    """Simple keyword search over graph.json when graphify CLI is unavailable."""
    graph_path = graphify_out / "graph.json"
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "Graph data unavailable."

    nodes = graph.get("nodes", [])
    keywords = question.lower().split()
    matches = []
    for node in nodes:
        label = str(node.get("label", "") or node.get("id", "")).lower()
        if any(kw in label for kw in keywords if len(kw) > 2):
            matches.append(node)

    if not matches:
        return f"No graph nodes matched: {question}"

    lines = []
    for node in matches[:10]:
        lines.append(
            f"- {node.get('label', node.get('id', '?'))}: "
            f"{str(node.get('description', ''))[:200]}"
        )
    result = "\n".join(lines)
    words = result.split()[:budget]
    return " ".join(words)


def create_graphify_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
) -> list:
    cfg = settings or get_settings()
    graphify_out = cfg.qa_agent_graphify_out
    budget = cfg.qa_agent_token_budget

    @tool
    def graphify_query(question: str, token_budget: int = 0) -> str:
        """Query the knowledge graph for relevant docs/code context. Budget-capped to save tokens."""
        effective_budget = token_budget or budget
        return query_graph(question, graphify_out, effective_budget, cost)

    @tool
    def graphify_path(source_concept: str, target_concept: str) -> str:
        """Trace path between two concepts in the knowledge graph (e.g. Controller → Service)."""
        return path_graph(source_concept, target_concept, graphify_out, cost)

    return [graphify_query, graphify_path]
