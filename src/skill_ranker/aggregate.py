"""Aggregate SkillEvents into per-day partials, and merge partials into window stats."""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from .models import (
    DURATION_BUCKETS_MS,
    REDACTED_SKILL_NAMES,
    DayPartial,
    SkillDayRow,
    TeamDayTotals,
    SkillEvent,
    SkillStats,
)


def duration_bucket(ms: float) -> int:
    return bisect.bisect_left(DURATION_BUCKETS_MS, ms)


def aggregate_day(day: date, events: Iterable[SkillEvent]) -> DayPartial:
    """Fold one day's events into additive counters. Events outside `day` are ignored."""
    part = DayPartial(day=day)

    def row(e: SkillEvent) -> SkillDayRow:
        key = (e.org_id, e.team, e.skill_name)
        r = part.skills.get(key)
        if r is None:
            r = part.skills[key] = SkillDayRow(org_id=e.org_id, team=e.team, skill_name=e.skill_name)
        return r

    def totals(e: SkillEvent) -> TeamDayTotals:
        t = part.team_totals.get((e.org_id, e.team))
        if t is None:
            t = part.team_totals[(e.org_id, e.team)] = TeamDayTotals()
        return t

    for e in events:
        if e.ts.date() != day:
            continue
        if e.kind == "prompt":
            totals(e).prompts += 1
            if e.user_key:
                k = (e.org_id, e.team, e.user_key)
                part.active_users[k] = part.active_users.get(k, 0) + 1
            continue
        if e.kind in ("api_other", "api_cost"):
            totals(e).cost_usd += e.cost_usd
            if e.kind == "api_other":
                continue
        r = row(e)
        if e.kind == "activation":
            r.invocations += 1
            r.slash_invocations += e.trigger == "user-slash"
            if e.skill_source and not r.skill_source:
                r.skill_source = e.skill_source
            if e.user_key:
                k = (e.org_id, e.team, e.skill_name, e.user_key)
                part.users[k] = part.users.get(k, 0) + 1
            if e.skill_name in REDACTED_SKILL_NAMES or e.skill_name == "unknown":
                part.unresolved_names += 1
        elif e.kind == "tool_result":
            if e.success is True:
                r.successes += 1
            elif e.success is False:
                r.failures += 1
                if e.error_type:
                    k = (e.org_id, e.team, e.skill_name, e.error_type)
                    part.errors[k] = part.errors.get(k, 0) + 1
            if e.duration_ms is not None:
                r.duration_hist[duration_bucket(e.duration_ms)] += 1
        elif e.kind == "decision":
            if e.decision == "accept":
                r.accepts += 1
            elif e.decision == "reject":
                r.rejects += 1
        elif e.kind == "api_cost":
            r.cost_usd += e.cost_usd
            r.input_tokens += e.input_tokens
            r.output_tokens += e.output_tokens
            r.cache_read_tokens += e.cache_read_tokens
    return part


def hist_percentile(hist: list[int], q: float) -> float | None:
    """Approximate percentile: upper bound of the bucket containing the q-th observation."""
    total = sum(hist)
    if not total:
        return None
    target = q * total
    seen = 0
    for i, n in enumerate(hist):
        seen += n
        if seen >= target:
            return float(DURATION_BUCKETS_MS[i]) if i < len(DURATION_BUCKETS_MS) else float("inf")
    return None


def merge_partials(partials: list[DayPartial], *, team: str | None = None,
                   org_id: str | None = None,
                   incomplete_days: frozenset[date] = frozenset()) -> list[SkillStats]:
    """Combine day partials into per-skill window stats (optionally filtered to a team/org).

    Days in `incomplete_days` count toward totals but are left out of trend/retention, so a
    half-finished "today" does not make every skill look like it is declining.
    """
    if not partials:
        return []

    def keep(o: str, t: str) -> bool:
        return (team is None or t == team) and (org_id is None or o == org_id)

    days = sorted(p.day for p in partials if p.day not in incomplete_days) or [min(p.day for p in partials)]
    last_day = days[-1]
    # Trend / retention compare the most recent 7 days against the 7 before (or half-windows
    # when the window is shorter than 14 days).
    span = max(1, min(7, len(days) // 2))
    recent_start = last_day - timedelta(days=span - 1)
    prev_start = recent_start - timedelta(days=span)

    acc: dict[str, dict] = defaultdict(lambda: {
        "source": "", "inv": 0, "slash": 0, "succ": 0, "fail": 0, "acc": 0, "rej": 0,
        "cost": 0.0, "in": 0, "out": 0, "hist": [0] * (len(DURATION_BUCKETS_MS) + 1),
        "users": set(), "teams": set(), "days": set(), "recent_inv": 0, "prev_inv": 0,
        "recent_users": set(), "prev_users": set(), "errors": defaultdict(int),
    })
    for p in partials:
        for (o, t, s), r in p.skills.items():
            if not keep(o, t):
                continue
            a = acc[s]
            a["source"] = a["source"] or r.skill_source
            a["inv"] += r.invocations
            a["slash"] += r.slash_invocations
            a["succ"] += r.successes
            a["fail"] += r.failures
            a["acc"] += r.accepts
            a["rej"] += r.rejects
            a["cost"] += r.cost_usd
            a["in"] += r.input_tokens
            a["out"] += r.output_tokens
            a["hist"] = [x + y for x, y in zip(a["hist"], r.duration_hist)]
            if r.invocations:
                a["teams"].add((o, t))
                a["days"].add(p.day)
                if p.day in incomplete_days or p.day > last_day:
                    pass
                elif p.day >= recent_start:
                    a["recent_inv"] += r.invocations
                elif p.day >= prev_start:
                    a["prev_inv"] += r.invocations
        for (o, t, s, u), _n in p.users.items():
            if not keep(o, t):
                continue
            a = acc[s]
            a["users"].add(u)
            if p.day in incomplete_days or p.day > last_day:
                pass
            elif p.day >= recent_start:
                a["recent_users"].add(u)
            elif p.day >= prev_start:
                a["prev_users"].add(u)
        for (o, t, s, err), n in p.errors.items():
            if keep(o, t):
                acc[s]["errors"][err] += n

    has_prev_period = days[0] <= prev_start
    out: list[SkillStats] = []
    for skill, a in acc.items():
        if not a["inv"] and not a["cost"]:
            continue
        trend = retention = None
        if has_prev_period:
            if a["prev_inv"]:
                trend = (a["recent_inv"] - a["prev_inv"]) / a["prev_inv"]
            elif a["recent_inv"]:
                trend = 1.0
            if a["prev_users"]:
                retention = len(a["prev_users"] & a["recent_users"]) / len(a["prev_users"])
        out.append(SkillStats(
            skill_name=skill, skill_source=a["source"], invocations=a["inv"],
            slash_invocations=a["slash"], unique_users=len(a["users"]),
            unique_teams=len(a["teams"]), active_days=len(a["days"]),
            successes=a["succ"], failures=a["fail"], accepts=a["acc"], rejects=a["rej"],
            cost_usd=a["cost"], input_tokens=a["in"], output_tokens=a["out"],
            p50_duration_ms=hist_percentile(a["hist"], 0.5),
            p95_duration_ms=hist_percentile(a["hist"], 0.95),
            retention=retention, trend=trend,
            top_errors=sorted(a["errors"].items(), key=lambda kv: -kv[1])[:5],
            retention_base=len(a["prev_users"]), prev_invocations=a["prev_inv"],
        ))
    return out
