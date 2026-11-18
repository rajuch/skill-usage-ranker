"""`skill-ranker` command line."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from .adapters import SignozError, build_adapter
from .cache import Cache, SaltMismatchError
from .config import Config, load_config
from .engine import Engine, RankResult
from .ranking import VIEWS, view_sort_key
from .report.build import ReportData, build_report
from .report.render import to_csv, to_json, write_html
from .window import parse_window

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Rank Claude Code skills across your organization from OTel telemetry.")
cache_app = typer.Typer(no_args_is_help=True, help="Inspect or manage the local SQLite cache.")
app.add_typer(cache_app, name="cache")
console = Console()

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", "-c", help="Path to ranker.yaml")]
InputOpt = Annotated[Optional[list[Path]], typer.Option(
    "--input", "-i", help="OTLP JSONL file/dir (overrides config; implies source otlp-jsonl)")]
WindowOpt = Annotated[str, typer.Option("--window", "-w", help="Relative window ending today, e.g. 7d, 30d")]
FromOpt = Annotated[Optional[str], typer.Option("--from", help="Start date YYYY-MM-DD (UTC)")]
ToOpt = Annotated[Optional[str], typer.Option("--to", help="End date YYYY-MM-DD (UTC, inclusive)")]


def _setup(config: Path | None, inputs: list[Path] | None) -> tuple[Config, Engine, Cache]:
    cfg = load_config(config)
    if inputs:
        cfg.source.type = "otlp-jsonl"
        cfg.source.paths = [str(p) for p in inputs]
    try:
        adapter = build_adapter(cfg)
        cache = Cache(cfg.cache.path)
        cache.check_salt(cfg.salt)
    except (RuntimeError, ValueError, NotImplementedError, SaltMismatchError) as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2) from e
    return cfg, Engine(cfg, adapter, cache), cache


def _fmt_pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def _fmt_trend(x: float | None) -> str:
    return "–" if x is None else f"{x:+.0%}"


@app.command()
def rank(
    config: ConfigOpt = None,
    inputs: InputOpt = None,
    window: WindowOpt = "30d",
    start: FromOpt = None,
    end: ToOpt = None,
    view: Annotated[str, typer.Option("--view", "-v", help=f"One of: {', '.join(VIEWS)}")] = "overall",
    team: Annotated[Optional[str], typer.Option(help="Only count usage from this team.id")] = None,
    top: Annotated[int, typer.Option("--top", "-n")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table")] = False,
    refresh: Annotated[bool, typer.Option(help="Recompute every day in the window")] = False,
) -> None:
    """Rank skills over a time window (uses the cache; only missing days are computed)."""
    cfg, engine, cache = _setup(config, inputs)
    with cache:
        try:
            w = parse_window(window, start, end)
            key = view_sort_key(view, cfg.ranking)
        except ValueError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(2) from e
        try:
            result = engine.rank(w, team=team, refresh=refresh)
        except SignozError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(1) from e
    rows = sorted(result.ranked, key=key)[:top]
    if as_json:
        print(json.dumps({"window": str(w), "view": view, "team": team,
                          "skills": [asdict(s) | {"success_rate": s.success_rate,
                                                  "cost_per_invocation": s.cost_per_invocation}
                                     for s in rows],
                          "below_threshold": result.info.below_threshold}, indent=2))
        return
    _print_table(result, rows, view, team)


def _print_table(result: RankResult, rows, view: str, team: str | None) -> None:
    info = result.info
    title = f"Skill ranking · {VIEWS[view]} · {info.window}" + (f" · team={team}" if team else "")
    t = Table(title=title, title_justify="left", header_style="bold")
    for col, kw in [("#", {"justify": "right"}), ("skill", {}), ("source", {"style": "dim"}),
                    ("score", {"justify": "right"}), ("uses", {"justify": "right"}),
                    ("/slash", {"justify": "right"}), ("users", {"justify": "right"}),
                    ("teams", {"justify": "right"}), ("success", {"justify": "right"}),
                    ("p95 ms", {"justify": "right"}), ("trend", {"justify": "right"}),
                    ("retention", {"justify": "right"}), ("cost $", {"justify": "right"})]:
        t.add_column(col, **kw)
    for i, s in enumerate(rows, 1):
        p95 = "–" if s.p95_duration_ms is None else ("60000+" if s.p95_duration_ms == float("inf")
                                                       else f"≤{s.p95_duration_ms:.0f}")
        t.add_row(str(i), s.skill_name, s.skill_source, f"{s.score:.3f}", str(s.invocations),
                  str(s.slash_invocations), str(s.unique_users), str(s.unique_teams),
                  _fmt_pct(s.success_rate), p95, _fmt_trend(s.trend), _fmt_pct(s.retention),
                  f"{s.cost_usd:.2f}")
    console.print(t)
    if not rows:
        console.print("[yellow]No skills met the minimum support threshold in this window.[/yellow]")
    src = ("ranking served from cache" if info.ranking_from_cache else
           f"{len(info.days_from_cache)} day(s) from cache, {len(info.days_computed)} computed")
    notes = [src]
    if info.incomplete_days:
        notes.append(f"{len(info.incomplete_days)} incomplete day(s) (short TTL)")
    if info.below_threshold:
        notes.append(f"{len(info.below_threshold)} skill(s) below min support")
    if info.unresolved_names:
        notes.append(f"[yellow]{info.unresolved_names} activation(s) with redacted names — "
                     "set OTEL_LOG_TOOL_DETAILS=1[/yellow]")
    console.print("[dim]" + " · ".join(notes) + "[/dim]")


def _build(config, inputs, window, start, end, team, refresh) -> tuple[Config, ReportData]:
    cfg, engine, cache = _setup(config, inputs)
    with cache:
        try:
            w = parse_window(window, start, end)
            data = build_report(engine, w, team=team, refresh=refresh)
        except ValueError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(2) from e
        except SignozError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(1) from e
    if team and data.summary.active_users < cfg.ranking.min_team_users:
        console.print(f"[red]error:[/red] team {team!r} has {data.summary.active_users} active user(s); "
                      f"reports need at least {cfg.ranking.min_team_users} (ranking.min_team_users) "
                      "so individuals cannot be identified")
        raise typer.Exit(2)
    return cfg, data


TeamOpt = Annotated[Optional[str], typer.Option(help="Only include usage from this team.id")]


@app.command()
def report(
    config: ConfigOpt = None,
    inputs: InputOpt = None,
    window: WindowOpt = "30d",
    start: FromOpt = None,
    end: ToOpt = None,
    team: TeamOpt = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output .html path")] = None,
    refresh: Annotated[bool, typer.Option(help="Recompute every day in the window")] = False,
) -> None:
    """Write a self-contained HTML report (one file, no external requests)."""
    _, data = _build(config, inputs, window, start, end, team, refresh)
    if out is None:
        suffix = f"-{team}" if team else ""
        out = Path("reports") / f"skills-{data.window.start}_{data.window.end}{suffix}.html"
    write_html(data, out)
    q = data.quality
    console.print(f"[green]wrote[/green] {out}  ({len(data.leaderboard)} ranked skills, "
                  f"{q.days_from_cache} day(s) from cache, {q.days_computed} computed)")


@app.command()
def export(
    config: ConfigOpt = None,
    inputs: InputOpt = None,
    window: WindowOpt = "30d",
    start: FromOpt = None,
    end: ToOpt = None,
    team: TeamOpt = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="csv or json")] = "csv",
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output path (default: stdout)")] = None,
    refresh: Annotated[bool, typer.Option(help="Recompute every day in the window")] = False,
) -> None:
    """Export the ranked skills as CSV or JSON."""
    if fmt not in ("csv", "json"):
        console.print("[red]error:[/red] --format must be csv or json")
        raise typer.Exit(2)
    _, data = _build(config, inputs, window, start, end, team, refresh)
    text = to_csv(data) if fmt == "csv" else to_json(data)
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {out}")


@app.command()
def doctor(config: ConfigOpt = None, inputs: InputOpt = None) -> None:
    """Check that the source is reachable and has usable skill telemetry."""
    cfg, engine, cache = _setup(config, inputs)
    cache.close()
    try:
        report = engine.adapter.healthcheck()
    except SignozError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from e
    icons = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
    for name, status, detail in report.checks:
        console.print(f"{icons[status]} {name}: {detail}")
    console.print(f"source: {engine.adapter.source_id}\ncache:  {cfg.cache.path}")
    raise typer.Exit(0 if report.ok else 1)


@cache_app.command("status")
def cache_status(config: ConfigOpt = None) -> None:
    """List cached days per source."""
    cfg = load_config(config)
    with Cache(cfg.cache.path) as cache:
        rows = cache.list_status()
    if not rows:
        console.print(f"cache is empty ({cfg.cache.path})")
        return
    t = Table(title=f"Cache · {cfg.cache.path}", title_justify="left")
    for c in ("source", "day", "complete", "computed at (UTC)", "schema", "unresolved"):
        t.add_column(c)
    for src, st in rows:
        t.add_row(src, st.day.isoformat(), "yes" if st.is_complete else "partial",
                  st.computed_at.strftime("%Y-%m-%d %H:%M"), str(st.schema_version),
                  str(st.unresolved_names))
    console.print(t)


@cache_app.command("refresh")
def cache_refresh(
    start: Annotated[str, typer.Option("--from", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--to", help="YYYY-MM-DD")],
    config: ConfigOpt = None,
    inputs: InputOpt = None,
) -> None:
    """Drop cached days in a date range so the next run recomputes them."""
    _, engine, cache = _setup(config, inputs)
    with cache:
        n = cache.invalidate_days(engine.adapter.source_id, date.fromisoformat(start), date.fromisoformat(end))
    console.print(f"invalidated {n} cached day(s) for {engine.adapter.source_id}")


@cache_app.command("clear")
def cache_clear(config: ConfigOpt = None,
                yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False) -> None:
    """Delete everything in the cache (all sources)."""
    cfg = load_config(config)
    if not yes:
        typer.confirm(f"Delete all cached data in {cfg.cache.path}?", abort=True)
    with Cache(cfg.cache.path) as cache:
        cache.clear()
    console.print("cache cleared")


if __name__ == "__main__":
    app()
