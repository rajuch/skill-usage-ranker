"""Composite scoring and ranking views."""

from __future__ import annotations

import math
from collections.abc import Callable

from .config import RankingConfig
from .models import SkillStats

VIEWS: dict[str, str] = {
    "overall": "composite score",
    "used": "invocations",
    "adopted": "distinct users, then teams",
    "growing": "recent vs previous period",
    "reliable": "smoothed success rate",
    "expensive": "total cost",
}


def percentile_ranks(values: list[float | None]) -> list[float]:
    """Map values to [0, 1] by rank (ties share the average rank). None -> 0.5 (neutral)."""
    present = sorted(v for v in values if v is not None)
    if not present:
        return [0.5] * len(values)
    if len(present) == 1:
        return [1.0 if v is not None else 0.5 for v in values]
    out = []
    for v in values:
        if v is None:
            out.append(0.5)
            continue
        lo = sum(1 for x in present if x < v)
        hi = sum(1 for x in present if x <= v) - 1
        out.append(((lo + hi) / 2) / (len(present) - 1))
    return out


def smoothed_rate(hits: int, total: int, prior: float, strength: float) -> float | None:
    """Bayesian average: shrink low-volume rates toward the org-wide prior."""
    if total == 0:
        return None
    return (hits + strength * prior) / (total + strength)


def eligible(stats: list[SkillStats], cfg: RankingConfig) -> tuple[list[SkillStats], list[SkillStats]]:
    keep, low = [], []
    for s in stats:
        (keep if s.invocations >= cfg.min_invocations and s.unique_users >= cfg.min_users else low).append(s)
    return keep, low


def score(stats: list[SkillStats], cfg: RankingConfig) -> list[SkillStats]:
    """Set `.score` on each skill (0..1-ish) and return them sorted best-first."""
    if not stats:
        return []
    w = cfg.weights
    succ_total = sum(s.successes for s in stats)
    succ_n = sum(s.successes + s.failures for s in stats)
    acc_total = sum(s.accepts for s in stats)
    acc_n = sum(s.accepts + s.rejects for s in stats)
    succ_prior = succ_total / succ_n if succ_n else 1.0
    acc_prior = acc_total / acc_n if acc_n else 1.0

    inv = percentile_ranks([math.log1p(s.invocations) for s in stats])
    users = percentile_ranks([float(s.unique_users) for s in stats])
    teams = percentile_ranks([float(s.unique_teams) for s in stats])
    ret = percentile_ranks([s.retention for s in stats])
    trend = percentile_ranks([s.trend for s in stats])
    cpi = percentile_ranks([s.cost_per_invocation for s in stats])

    for i, s in enumerate(stats):
        sr = smoothed_rate(s.successes, s.successes + s.failures, succ_prior, cfg.smoothing_strength)
        ar = smoothed_rate(s.accepts, s.accepts + s.rejects, acc_prior, cfg.smoothing_strength)
        s.score = (
            w.invocations * inv[i]
            + w.unique_users * users[i]
            + w.unique_teams * teams[i]
            + w.success_rate * (sr if sr is not None else succ_prior)
            + w.acceptance_rate * (ar if ar is not None else acc_prior)
            + w.retention * ret[i]
            + w.trend * trend[i]
            - w.cost_per_invocation * cpi[i]
        )
    return sorted(stats, key=lambda s: (-s.score, -s.invocations, s.skill_name))


def view_sort_key(view: str, cfg: RankingConfig) -> Callable[[SkillStats], tuple]:
    def success(s: SkillStats) -> float:
        return smoothed_rate(s.successes, s.successes + s.failures, 1.0, cfg.smoothing_strength) or 0.0

    keys: dict[str, Callable[[SkillStats], tuple]] = {
        "overall": lambda s: (-s.score, -s.invocations, s.skill_name),
        "used": lambda s: (-s.invocations, -s.unique_users, s.skill_name),
        "adopted": lambda s: (-s.unique_users, -s.unique_teams, s.skill_name),
        "growing": lambda s: (s.trend is None, -(s.trend or 0.0), -s.invocations, s.skill_name),
        "reliable": lambda s: (-success(s), -s.invocations, s.skill_name),
        "expensive": lambda s: (-s.cost_usd, s.skill_name),
    }
    if view not in keys:
        raise ValueError(f"unknown view {view!r}; choose from {', '.join(VIEWS)}")
    return keys[view]
