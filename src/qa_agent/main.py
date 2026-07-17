from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from qa_agent.agent import run_generate, run_generate_from_pr
from qa_agent.config import get_settings
from qa_agent.indexing import run_index
from qa_agent.tools.output import DRY_RUN_TEMPLATE, extract_gherkin_from_text, validate_gherkin, write_feature_files
from qa_agent.models.schemas import GeneratedFeature, slugify
from qa_agent.cost import CostTracker
from qa_agent.models.llm import describe_model_endpoint, validate_model_credentials

load_dotenv()

app = typer.Typer(
    name="qa-agent",
    help="QA Agent: Outline + GitHub PR → Gherkin test cases",
    no_args_is_help=True,
)
console = Console()


@app.command()
def models() -> None:
    """Show active model profile, resolved models, and base URLs."""
    settings = get_settings()
    missing = validate_model_credentials(settings)

    research = describe_model_endpoint(settings, "research")
    generate = describe_model_endpoint(settings, "generate")
    nano = describe_model_endpoint(settings, "nano")
    pro = describe_model_endpoint(settings, "pro")

    nano_note = "" if settings.qa_agent_nano_model else " (fallback → research)"
    pro_note = "" if settings.qa_agent_pro_model else " (fallback → generate; escalation off)"

    console.print(Panel(
        f"Profile: {settings.qa_agent_model_profile}\n\n"
        f"[bold]Nano[/bold]{nano_note}\n"
        f"  Model: {nano['model_ref']}\n"
        f"  Base URL: {nano['base_url']}\n\n"
        f"[bold]Research[/bold]\n"
        f"  Model: {research['model_ref']}\n"
        f"  Base URL: {research['base_url']}\n\n"
        f"[bold]Generate[/bold]\n"
        f"  Model: {generate['model_ref']}\n"
        f"  Base URL: {generate['base_url']}\n\n"
        f"[bold]Pro[/bold]{pro_note}\n"
        f"  Model: {pro['model_ref']}\n"
        f"  Base URL: {pro['base_url']}",
        title="Model Configuration",
        border_style="cyan",
    ))
    console.print(
        f"[dim]Query expansion: {'on' if settings.qa_agent_query_expansion else 'off'} · "
        f"Triage: {'on' if settings.qa_agent_triage else 'off'} · "
        f"Escalation: {'on' if settings.qa_agent_escalation else 'off'} · "
        f"Embeddings: {settings.qa_agent_embedding_model or 'off'}[/dim]"
    )

    if settings.llm_base_url:
        console.print(f"[dim]Global LLM_BASE_URL: {settings.llm_base_url}[/dim]")

    if missing:
        console.print(f"[yellow]Missing:[/yellow] {', '.join(missing)}")
    else:
        console.print("[green]All required credentials are set.[/green]")


@app.command()
def index(
    outline_sync: bool = typer.Option(False, "--outline-sync", help="Sync Outline docs to corpus/"),
    github_clone: bool = typer.Option(False, "--github-clone", help="Clone/pull GitHub repos to corpus/"),
    with_graph: bool = typer.Option(
        False,
        "--with-graph",
        help="Build graphify knowledge graph (slow — 100+ docs can take 10+ min)",
    ),
) -> None:
    """Build or update the knowledge graph index from Outline docs and GitHub code."""
    settings = get_settings()
    console.print("[bold]Indexing corpus...[/bold]")

    if not outline_sync and not github_clone and not with_graph:
        console.print("[yellow]Tip:[/yellow] Use --outline-sync and/or --github-clone")
        console.print("[dim]Graph build skipped by default (fast). Add --with-graph for full graphify.[/dim]")

    result = run_index(
        outline_sync=outline_sync,
        github_clone=github_clone,
        with_graph=with_graph,
        settings=settings,
    )

    graph_note = "skipped (fast mode)" if result.get("graph_skipped") else "built"
    console.print(Panel(
        f"Docs synced: {result['docs_synced']}\n"
        f"Code dirs: {', '.join(result.get('code_dirs', [])) or 'none'}\n"
        f"Graph: {graph_note}",
        title="Index Complete",
        border_style="green",
    ))


@app.command()
def generate(
    query: str = typer.Argument(..., help="QA request / feature description"),
    budget: int = typer.Option(1500, "--budget", help="Token budget for graphify query"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Output directory"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate without LLM (template only)"),
) -> None:
    """Generate Gherkin test cases from Outline docs and GitHub PRs."""
    settings = get_settings()
    if output_dir:
        settings.qa_agent_output_dir = output_dir

    settings.qa_agent_output_dir.mkdir(parents=True, exist_ok=True)

    console.print(Panel(query, title="QA Request", border_style="blue"))

    if dry_run:
        _dry_run_generate(query, settings)
        return

    missing = validate_model_credentials(settings)
    if missing:
        console.print(
            f"[red]Error:[/red] Missing API keys for profile "
            f"'{settings.qa_agent_model_profile}': {', '.join(missing)}"
        )
        console.print("Use --dry-run for template-only output, or run: qa-agent models")
        raise typer.Exit(1)

    console.print(Panel(
        f"Profile: {settings.qa_agent_model_profile}\n"
        f"Research: {settings.active_research_model()}\n"
        f"  → {describe_model_endpoint(settings, 'research')['base_url']}\n"
        f"Generate: {settings.active_generate_model()}\n"
        f"  → {describe_model_endpoint(settings, 'generate')['base_url']}",
        title="Models",
        border_style="cyan",
    ))

    console.print("[bold]Running QA Agent...[/bold]")
    existing_features = {p.name for p in settings.qa_agent_output_dir.glob("*.feature")}
    try:
        output = run_generate(query, settings=settings, budget=budget)
    except Exception as exc:
        console.print(f"[red]Agent error:[/red] {exc}")
        raise typer.Exit(1) from exc

    cost = output["cost"]
    console.print(Panel(
        f"LLM input tokens: {cost.llm_input_tokens}\n"
        f"LLM output tokens: {cost.llm_output_tokens}\n"
        f"Graphify tokens: {cost.graphify_query_tokens}\n"
        f"Outline API calls: {cost.outline_api_calls}\n"
        f"GitHub API calls: {cost.github_api_calls}\n"
        f"Estimated cost: ${cost.estimated_usd:.4f}\n"
        f"Sources: {', '.join(cost.sources_used) or 'none'}",
        title="Cost Report",
        border_style="yellow",
    ))

    console.print("\n[bold green]Agent response:[/bold green]")
    console.print(output["response"][:2000])

    # Report the feature file created during this run (newest by mtime, not name).
    feature_files = sorted(
        settings.qa_agent_output_dir.glob("*.feature"),
        key=lambda p: p.stat().st_mtime,
    )
    new_files = [p for p in feature_files if p.name not in existing_features]
    latest = new_files[-1] if new_files else (feature_files[-1] if feature_files else None)

    if output.get("write_blocked") and not new_files:
        console.print(
            "\n[yellow]No feature file was written — generation was blocked by validation.[/yellow]"
        )
        if output.get("blocked_output"):
            console.print(output["blocked_output"])
    elif latest:
        console.print(f"\n[green]Feature file:[/green] {latest}")


def _dry_run_generate(query: str, settings) -> None:
    """Generate a template feature file without calling LLM."""
    cost = CostTracker(budget=settings.qa_agent_token_budget)
    template = DRY_RUN_TEMPLATE.format(query=query.replace('"', '\\"'))
    feature = GeneratedFeature(
        slug=slugify(query),
        feature_content=template,
        query=query,
    )
    paths = write_feature_files(feature, settings.qa_agent_output_dir, cost)
    meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
    console.print(f"[green]Dry-run feature written:[/green] {paths['feature']}")
    if meta.get("validation_errors"):
        console.print(f"[yellow]Validation:[/yellow] {meta['validation_errors']}")
    if meta.get("quality_warnings"):
        console.print(f"[yellow]Quality:[/yellow] {meta['quality_warnings']}")


@app.command(name="from-pr")
def from_pr(
    repo: str = typer.Argument(..., help="owner/repo, e.g. owner/web"),
    number: int = typer.Argument(..., help="Pull request number"),
    budget: int = typer.Option(1500, "--budget", help="Token budget for graphify query"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Output directory"),
) -> None:
    """Generate Gherkin test cases directly from a specific GitHub pull request."""
    settings = get_settings()
    if output_dir:
        settings.qa_agent_output_dir = output_dir
    settings.qa_agent_output_dir.mkdir(parents=True, exist_ok=True)

    missing = validate_model_credentials(settings)
    if missing:
        console.print(f"[red]Error:[/red] Missing API keys: {', '.join(missing)}")
        raise typer.Exit(1)

    console.print(Panel(f"{repo} #{number}", title="QA from PR", border_style="blue"))
    existing = {p.name for p in settings.qa_agent_output_dir.glob("*.feature")}
    try:
        output = run_generate_from_pr(repo, number, settings=settings, budget=budget)
    except Exception as exc:
        console.print(f"[red]Agent error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print("\n[bold green]Agent response:[/bold green]")
    console.print(output["response"][:2000])
    new_files = [
        p
        for p in sorted(settings.qa_agent_output_dir.glob("*.feature"), key=lambda p: p.stat().st_mtime)
        if p.name not in existing
    ]
    if new_files:
        console.print(f"\n[green]Feature file:[/green] {new_files[-1]}")
    elif output.get("write_blocked"):
        console.print("\n[yellow]Generation blocked by validation.[/yellow]")
        if output.get("blocked_output"):
            console.print(output["blocked_output"])


@app.command()
def trace(
    feature_file: Path = typer.Argument(..., help="Path to a generated .feature file"),
    fmt: str = typer.Option("both", "--format", help="csv | html | both"),
) -> None:
    """Build a traceability matrix (acceptance ID -> scenario -> source) for a feature file."""
    from qa_agent.reporting import build_traceability, traceability_csv, traceability_html

    if not feature_file.exists():
        console.print(f"[red]Not found:[/red] {feature_file}")
        raise typer.Exit(1)

    content = feature_file.read_text(encoding="utf-8")
    meta_path = feature_file.with_name(feature_file.stem + ".meta.json")
    checklist: list[dict] = []
    sources: list[dict] = []
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        checklist = meta.get("acceptance_checklist", [])
        sources = meta.get("sources", [])

    rows = build_traceability(content, checklist, sources)
    covered = sum(1 for r in rows if r["status"] == "covered")
    written: list[str] = []
    if fmt in {"csv", "both"}:
        csv_path = feature_file.with_name(feature_file.stem + ".trace.csv")
        csv_path.write_text(traceability_csv(rows), encoding="utf-8")
        written.append(str(csv_path))
    if fmt in {"html", "both"}:
        html_path = feature_file.with_name(feature_file.stem + ".trace.html")
        html_path.write_text(
            traceability_html(rows, title=f"Traceability — {feature_file.stem}"),
            encoding="utf-8",
        )
        written.append(str(html_path))

    console.print(Panel(
        f"Criteria: {len(rows)} · Covered: {covered}\n" + "\n".join(written),
        title="Traceability Matrix",
        border_style="green",
    ))


@app.command(name="coverage-gap")
def coverage_gap_cmd(
    feature_file: Path = typer.Argument(..., help="Path to a generated .feature file"),
    against: Optional[Path] = typer.Option(None, "--against", help="Directory of existing tests (default: corpus/code)"),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Report which generated scenarios are likely already covered by existing repo tests."""
    from qa_agent.reporting import coverage_gap, scan_existing_tests

    settings = get_settings()
    if not feature_file.exists():
        console.print(f"[red]Not found:[/red] {feature_file}")
        raise typer.Exit(1)

    target = against or settings.corpus_code_dir
    content = feature_file.read_text(encoding="utf-8")
    existing = scan_existing_tests(target)
    report = coverage_gap(content, existing)

    if as_json:
        console.print_json(json.dumps(report))
        return

    console.print(Panel(
        f"Scenarios: {report['total_scenarios']} · "
        f"Existing tests scanned: {report['existing_tests_scanned']} · "
        f"Likely new (gap): {report['gap_count']}",
        title=f"Coverage Gap vs {target}",
        border_style="cyan",
    ))
    if report["new_scenarios"]:
        console.print("[bold]New / uncovered scenarios:[/bold]")
        for entry in report["new_scenarios"]:
            console.print(f"  [yellow]•[/yellow] {entry['scenario']}")
    if report["likely_covered"]:
        console.print("\n[dim]Likely already covered:[/dim]")
        for entry in report["likely_covered"]:
            console.print(f"  [green]✓[/green] {entry['scenario']}  ~ {entry['best_match']} ({entry['score']})")


@app.command()
def eval(
    cases: Path = typer.Option(Path("evals/cases.json"), "--cases", help="Path to eval cases JSON"),
    live: bool = typer.Option(False, "--live", help="Run the agent for cases that define a query"),
    budget: int = typer.Option(1500, "--budget", help="Token budget for live runs"),
) -> None:
    """Run the quality-gate eval suite against fixtures (offline) or live queries."""
    from qa_agent.eval import run_eval

    if not cases.exists():
        console.print(f"[red]Cases file not found:[/red] {cases}")
        raise typer.Exit(1)

    settings = get_settings()
    summary = run_eval(cases, live=live, settings=settings, budget=budget)

    for result in summary["results"]:
        if result.get("skipped"):
            console.print(f"[dim]- {result['name']}: skipped (live-only)[/dim]")
            continue
        status = "[green]PASS[/green]" if result["passed"] else "[red]FAIL[/red]"
        console.print(f"{status} {result['name']}  (scenarios={result.get('scenarios')}, criteria={result.get('criteria')})")
        for failure in result.get("failures", []):
            console.print(f"    [red]×[/red] {failure}")

    console.print(Panel(
        f"Passed {summary['passed']}/{summary['total']}",
        title="Eval Summary",
        border_style="green" if summary["all_passed"] else "red",
    ))
    if not summary["all_passed"]:
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8787, "--port", help="Port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
) -> None:
    """Start the web UI server."""
    import uvicorn

    console.print(Panel(
        f"Web UI: [link=http://{host}:{port}]http://{host}:{port}[/link]\n"
        f"API docs: http://{host}:{port}/api/docs",
        title="QA Agent Server",
        border_style="green",
    ))
    uvicorn.run(
        "qa_agent.server:app",
        host=host,
        port=port,
        reload=reload,
    )


@app.command()
def validate(
    feature_file: Path = typer.Argument(..., help="Path to .feature file"),
) -> None:
    """Validate Gherkin syntax of a feature file."""
    content = feature_file.read_text(encoding="utf-8")
    gherkin = extract_gherkin_from_text(content)
    errors = validate_gherkin(gherkin)
    if errors:
        console.print(f"[red]Invalid:[/red] {', '.join(errors)}")
        raise typer.Exit(1)
    console.print("[green]Valid Gherkin[/green]")


if __name__ == "__main__":
    app()
