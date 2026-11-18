"""Day partials must merge to exactly what a direct computation over the window gives."""

from collections import defaultdict

from conftest import SALT

from skill_ranker.adapters import OtlpJsonlAdapter
from skill_ranker.aggregate import aggregate_day, hist_percentile, merge_partials
from skill_ranker.models import DURATION_BUCKETS_MS


def test_merged_partials_equal_direct_computation(synthetic_dir):
    adapter = OtlpJsonlAdapter([synthetic_dir], SALT)
    days = sorted(adapter._load())
    events_by_day = {d: adapter.fetch_day(d) for d in days}
    merged = {s.skill_name: s for s in merge_partials(
        [aggregate_day(d, evs) for d, evs in events_by_day.items()])}

    inv, users, teams, succ, fail, cost = (defaultdict(int), defaultdict(set), defaultdict(set),
                                           defaultdict(int), defaultdict(int), defaultdict(float))
    for evs in events_by_day.values():
        for e in evs:
            if e.kind == "activation":
                inv[e.skill_name] += 1
                users[e.skill_name].add(e.user_key)
                teams[e.skill_name].add(e.team)
            elif e.kind == "tool_result":
                succ[e.skill_name] += e.success is True
                fail[e.skill_name] += e.success is False
            elif e.kind == "api_cost":
                cost[e.skill_name] += e.cost_usd

    assert set(merged) == set(inv)
    for name, s in merged.items():
        assert s.invocations == inv[name]
        # distinct users across days must not be double counted
        assert s.unique_users == len(users[name])
        assert s.unique_teams == len(teams[name])
        assert s.successes == succ[name] and s.failures == fail[name]
        assert abs(s.cost_usd - cost[name]) < 1e-9


def test_team_filter(synthetic_dir):
    adapter = OtlpJsonlAdapter([synthetic_dir], SALT)
    partials = [aggregate_day(d, adapter.fetch_day(d)) for d in sorted(adapter._load())]
    everyone = {s.skill_name: s.invocations for s in merge_partials(partials)}
    payments = {s.skill_name: s.invocations for s in merge_partials(partials, team="payments")}
    assert payments and all(payments[k] <= everyone[k] for k in payments)
    assert sum(payments.values()) < sum(everyone.values())


def test_growth_and_decline_detected(synthetic_dir):
    adapter = OtlpJsonlAdapter([synthetic_dir], SALT)
    stats = {s.skill_name: s for s in merge_partials(
        [aggregate_day(d, adapter.fetch_day(d)) for d in sorted(adapter._load())])}
    # generator: api-docs-gen grows 6%/day, legacy-deploy declines 5%/day
    assert stats["api-docs-gen"].trend > stats["legacy-deploy"].trend
    assert stats["legacy-deploy"].trend < 0


def test_events_outside_day_are_ignored(synthetic_dir):
    adapter = OtlpJsonlAdapter([synthetic_dir], SALT)
    d0, d1 = sorted(adapter._load())[:2]
    part = aggregate_day(d0, adapter.fetch_day(d1))
    assert part.skills == {}


def test_hist_percentile():
    hist = [0] * (len(DURATION_BUCKETS_MS) + 1)
    hist[0] = 90  # <=10ms
    hist[5] = 10  # <=1000ms
    assert hist_percentile(hist, 0.5) == 10.0
    assert hist_percentile(hist, 0.95) == 1000.0
    assert hist_percentile([0] * len(hist), 0.5) is None


def test_incomplete_day_excluded_from_trend(synthetic_dir):
    """A nearly empty 'today' must not drag every skill's trend negative."""
    from datetime import timedelta

    from skill_ranker.models import DayPartial

    adapter = OtlpJsonlAdapter([synthetic_dir], SALT)
    days = sorted(adapter._load())
    partials = [aggregate_day(d, adapter.fetch_day(d)) for d in days]
    today = days[-1] + timedelta(days=1)
    tiny_today = aggregate_day(today, [])  # no data yet
    base = {s.skill_name: s.trend for s in merge_partials(partials)}
    with_today = {s.skill_name: s.trend for s in merge_partials(
        partials + [tiny_today], incomplete_days=frozenset({today}))}
    assert base == with_today
    naive = {s.skill_name: s.trend for s in merge_partials(partials + [DayPartial(day=today)])}
    assert naive != base  # proves the exclusion matters
