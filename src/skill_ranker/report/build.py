"""Build the organization report's data (pure computation; rendering lives in render.py)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..aggregate import merge_partials
from ..config import RankingConfig
from ..engine import Engine
from ..models import DayPartial, SkillStats
from ..ranking import eligible, score, smoothed_rate, view_sort_key
from ..window import Window

HIGHLIGHT_N = 5

# "Needs attention" thresholds
DECLINE_TREND = -0.30
DECLINE_MIN_USES = 20
LOW_SUCCESS_SERIOUS = 0.85
LOW_SUCCESS_CRITICAL = 0.70
MIN_RESULTS_FOR_SUCCESS = 10
LOW_ACCEPTANCE = 0.90
MIN_DECISIONS = 10
LOW_RETENTION = 0.30
MIN_RETENTION_BASE = 5  # previous-period users needed before retention is judged
MIN_GROWTH_BASE = 20  # previous-period uses needed before a growth rate is shown


@dataclass(slots=True)
class DayPoint:
    day: date
    invocations: int
    complete: bool


@dataclass(slots=True)
class Summary:
    invocations: int
    slash_share: float | None
    skill_users: int
    active_users: int
    adoption: float | None  # skill users / Claude Code users
    skills_used: int
    prompts: int
    skill_prompt_share: float | None  # invocations / prompts
    total_cost: float
    skill_cost: float
    skill_cost_share: float | None
    trend: float | None  # recent period vs previous, complete days only
    trend_span_days: int


@dataclass(slots=True)
class LeaderRow:
    rank: int
    stats: SkillStats
    spark: list[int]  # daily invocations over the window's complete days
    success_smoothed: float | None


@dataclass(slots=True)
class AttentionItem:
    severity: str  # "warning" | "serious" | "critical"
    skill: str
    reason: str
    detail: str


@dataclass(slots=True)
class TeamRow:
    team: str
    active_users: int
    skill_users: int
    adoption: float | None
    invocations: int
    prompts: int
    top_skills: list[tuple[str, int]]
    is_rollup: bool = False
    teams_in_rollup: int = 0


@dataclass(slots=True)
class SourceRow:
    source: str
    skills: int
    invocations: int
    share: float


@dataclass(slots=True)
class Quality:
    days: int
    days_from_cache: int
    days_computed: int
    incomplete_days: list[date]
    unresolved_names: int
    unresolved_share: float | None
    below_threshold: list[str]
    min_invocations: int
    min_users: int
    min_team_users: int
    hidden_teams: int


@dataclass(slots=True)
class ReportData:
    title: str
    window: Window
    team: str | None
    source_id: str
    generated_at: datetime
    summary: Summary
    daily: list[DayPoint]
    leaderboard: list[LeaderRow]
    highlights: dict[str, list[LeaderRow]]
    attention: list[AttentionItem]
    teams: list[TeamRow]
    sources: list[SourceRow]
    quality: Quality
    days: list[date] = field(default_factory=list)


def _safe_div(a: float, b: float) -> float | None:
    return a / b if b else None


def _org_totals(partials: list[DayPartial], team: str | None):
    users: set[str] = set()
    prompts = 0
    cost = 0.0
    per_team_users: dict[str, set[str]] = defaultdict(set)
    per_team_prompts: dict[str, int] = defaultdict(int)
    for p in partials:
        for (_o, t, u) in p.active_users:
            if team is None or t == team:
                users.add(u)
                per_team_users[t].add(u)
        for (_o, t), v in p.team_totals.items():
            if team is None or t == team:
                prompts += v.prompts
                cost += v.cost_usd
                per_team_prompts[t] += v.prompts
    return users, prompts, cost, per_team_users, per_team_prompts


def _attention(ranked: list[SkillStats], cfg: RankingConfig) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    for s in ranked:
        n_results = s.successes + s.failures
        sr = s.success_rate
        if sr is not None and n_results >= MIN_RESULTS_FOR_SUCCESS:
            sev = ("critical" if sr < LOW_SUCCESS_CRITICAL else
                   "serious" if sr < LOW_SUCCESS_SERIOUS else None)
            if sev:
                errs = ", ".join(f"{e} ×{n}" for e, n in s.top_errors[:3]) or "no error type reported"
                items.append(AttentionItem(sev, s.skill_name, "Low success rate",
                                           f"{sr:.0%} of {n_results} runs succeeded · {errs}"))
        if (s.trend is not None and s.trend <= DECLINE_TREND
                and s.prev_invocations >= DECLINE_MIN_USES):
            items.append(AttentionItem("warning", s.skill_name, "Usage declining",
                                       f"{s.trend:+.0%} vs the previous period"))
        ar = s.acceptance_rate
        if ar is not None and (s.accepts + s.rejects) >= MIN_DECISIONS and ar < LOW_ACCEPTANCE:
            items.append(AttentionItem("warning", s.skill_name, "Often rejected",
                                       f"users rejected {1 - ar:.0%} of {s.accepts + s.rejects} invocations"))
        if (s.retention is not None and s.retention < LOW_RETENTION
                and s.retention_base >= MIN_RETENTION_BASE):
            items.append(AttentionItem("warning", s.skill_name, "Low retention",
                                       f"{s.retention:.0%} of last period's users came back"))
    order = {"critical": 0, "serious": 1, "warning": 2}
    return sorted(items, key=lambda i: (order[i.severity], i.skill, i.reason))


def build_report(engine: Engine, window: Window, *, team: str | None = None,
                 refresh: bool = False, title: str = "Claude Code skill usage") -> ReportData:
    cfg = engine.cfg.ranking
    partials, info = engine.load_partials(window, refresh=refresh)
    incomplete = frozenset(info.incomplete_days)
    complete_days = [p.day for p in partials if p.day not in incomplete]

    stats = merge_partials(partials, team=team, incomplete_days=incomplete)
    keep, low = eligible(stats, cfg)
    ranked = score(keep, cfg)
    info.below_threshold = sorted(s.skill_name for s in low)

    # daily invocation series (whole org/team and per skill)
    per_skill_day: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    daily_totals: dict[date, int] = defaultdict(int)
    for p in partials:
        for (_o, t, s), r in p.skills.items():
            if team is None or t == team:
                per_skill_day[s][p.day] += r.invocations
                daily_totals[p.day] += r.invocations
    daily = [DayPoint(p.day, daily_totals.get(p.day, 0), p.day not in incomplete) for p in partials]

    def row(i: int, s: SkillStats) -> LeaderRow:
        return LeaderRow(i, s, [per_skill_day[s.skill_name].get(d, 0) for d in complete_days],
                         smoothed_rate(s.successes, s.successes + s.failures, 1.0, cfg.smoothing_strength))

    leaderboard = [row(i, s) for i, s in enumerate(ranked, 1)]
    by_name = {r.stats.skill_name: r for r in leaderboard}

    def top(view: str, pred=lambda s: True) -> list[LeaderRow]:
        return [by_name[s.skill_name] for s in sorted(ranked, key=view_sort_key(view, cfg))
                if pred(s)][:HIGHLIGHT_N]

    highlights = {
        "adopted": top("adopted"),
        "growing": top("growing", lambda s: s.trend is not None and s.trend > 0
                       and s.prev_invocations >= MIN_GROWTH_BASE),
        "reliable": top("reliable", lambda s: (s.successes + s.failures) >= MIN_RESULTS_FOR_SUCCESS),
        "expensive": top("expensive", lambda s: s.cost_usd > 0),
    }

    # org / team totals
    active, prompts, total_cost, team_users, team_prompts = _org_totals(partials, team)
    skill_users: set[str] = set()
    team_skill_users: dict[str, set[str]] = defaultdict(set)
    team_skill_inv: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in partials:
        for (_o, t, s, u) in p.users:
            if team is None or t == team:
                skill_users.add(u)
                team_skill_users[t].add(u)
        for (_o, t, s), r in p.skills.items():
            if (team is None or t == team) and r.invocations:
                team_skill_inv[t][s] += r.invocations

    invocations = sum(s.invocations for s in stats)
    slash = sum(s.slash_invocations for s in stats)
    skill_cost = sum(s.cost_usd for s in stats)

    # overall trend on complete days (same periods as per-skill trend)
    span = max(1, min(7, len(complete_days) // 2))
    trend = None
    if len(complete_days) >= 2 * span and complete_days:
        last = complete_days[-1]
        recent = sum(daily_totals.get(last - timedelta(days=i), 0) for i in range(span))
        prev = sum(daily_totals.get(last - timedelta(days=span + i), 0) for i in range(span))
        trend = _safe_div(recent - prev, prev)

    summary = Summary(
        invocations=invocations, slash_share=_safe_div(slash, invocations),
        skill_users=len(skill_users), active_users=len(active),
        adoption=_safe_div(len(skill_users), len(active)), skills_used=len(stats),
        prompts=prompts, skill_prompt_share=_safe_div(invocations, prompts),
        total_cost=total_cost, skill_cost=skill_cost,
        skill_cost_share=_safe_div(skill_cost, total_cost), trend=trend, trend_span_days=span)

    # team breakdown, with small teams folded together (privacy)
    teams: list[TeamRow] = []
    small = TeamRow("Other teams", 0, 0, None, 0, 0, [], is_rollup=True)
    small_users: set[str] = set()
    small_skill_users: set[str] = set()
    small_skills: dict[str, int] = defaultdict(int)
    for t in sorted(set(team_users) | set(team_skill_inv)):
        n_active = len(team_users.get(t, set()))
        inv = sum(team_skill_inv[t].values())
        if n_active < cfg.min_team_users:
            small.teams_in_rollup += 1
            small_users |= team_users.get(t, set())
            small_skill_users |= team_skill_users.get(t, set())
            small.invocations += inv
            small.prompts += team_prompts.get(t, 0)
            for s, n in team_skill_inv[t].items():
                small_skills[s] += n
            continue
        teams.append(TeamRow(
            team=t, active_users=n_active, skill_users=len(team_skill_users.get(t, set())),
            adoption=_safe_div(len(team_skill_users.get(t, set())), n_active), invocations=inv,
            prompts=team_prompts.get(t, 0),
            top_skills=sorted(team_skill_inv[t].items(), key=lambda kv: (-kv[1], kv[0]))[:3]))
    teams.sort(key=lambda r: (-(r.adoption or 0), -r.invocations, r.team))
    hidden = small.teams_in_rollup
    if hidden:
        small.active_users = len(small_users)
        small.skill_users = len(small_skill_users)
        small.adoption = _safe_div(small.skill_users, small.active_users)
        small.top_skills = sorted(small_skills.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        if small.active_users >= cfg.min_team_users:
            teams.append(small)

    by_source: dict[str, list[SkillStats]] = defaultdict(list)
    for s in stats:
        by_source[s.skill_source or "unknown"].append(s)
    sources = sorted(
        (SourceRow(src, len(v), sum(x.invocations for x in v),
                   _safe_div(sum(x.invocations for x in v), invocations) or 0.0)
         for src, v in by_source.items()), key=lambda r: (-r.invocations, r.source))

    quality = Quality(
        days=len(partials), days_from_cache=len(info.days_from_cache),
        days_computed=len(info.days_computed), incomplete_days=list(info.incomplete_days),
        unresolved_names=info.unresolved_names,
        unresolved_share=_safe_div(info.unresolved_names, invocations),
        below_threshold=info.below_threshold, min_invocations=cfg.min_invocations,
        min_users=cfg.min_users, min_team_users=cfg.min_team_users, hidden_teams=hidden)

    return ReportData(
        title=title, window=window, team=team, source_id=engine.adapter.source_id,
        generated_at=engine.now.astimezone(UTC), summary=summary, daily=daily,
        leaderboard=leaderboard, highlights=highlights, attention=_attention(ranked, cfg),
        teams=teams, sources=sources, quality=quality, days=complete_days)

