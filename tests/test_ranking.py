from skill_ranker.config import RankingConfig
from skill_ranker.models import SkillStats
from skill_ranker.ranking import eligible, percentile_ranks, score, smoothed_rate, view_sort_key


def stats(name, inv=10, users=3, teams=1, succ=None, fail=0, cost=0.0, trend=None, retention=None):
    succ = inv if succ is None else succ
    return SkillStats(skill_name=name, skill_source="", invocations=inv, slash_invocations=0,
                      unique_users=users, unique_teams=teams, active_days=1, successes=succ,
                      failures=fail, accepts=0, rejects=0, cost_usd=cost, input_tokens=0,
                      output_tokens=0, p50_duration_ms=None, p95_duration_ms=None,
                      retention=retention, trend=trend)


def test_percentile_ranks_handles_ties_and_missing():
    assert percentile_ranks([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]
    assert percentile_ranks([1.0, 1.0, 3.0]) == [0.25, 0.25, 1.0]
    assert percentile_ranks([None, 2.0, 4.0]) == [0.5, 0.0, 1.0]


def test_smoothing_prevents_tiny_samples_winning():
    prior = 0.9
    tiny = smoothed_rate(2, 2, prior, 10)  # 2/2
    big = smoothed_rate(950, 1000, prior, 10)  # 95%
    assert big > tiny


def test_min_support_threshold():
    cfg = RankingConfig(min_invocations=5, min_users=2)
    keep, low = eligible([stats("a"), stats("b", inv=3), stats("c", users=1)], cfg)
    assert [s.skill_name for s in keep] == ["a"]
    assert {s.skill_name for s in low} == {"b", "c"}


def test_more_adoption_ranks_higher():
    cfg = RankingConfig()
    ranked = score([stats("niche", inv=10, users=2, teams=1),
                    stats("popular", inv=500, users=40, teams=5)], cfg)
    assert ranked[0].skill_name == "popular"


def test_failures_lower_score():
    cfg = RankingConfig()
    a, b = stats("ok", inv=100, succ=100), stats("flaky", inv=100, succ=50, fail=50)
    ranked = score([a, b], cfg)
    assert ranked[0].skill_name == "ok" and ranked[0].score > ranked[1].score


def test_views():
    cfg = RankingConfig()
    s = score([stats("cheap", inv=100, cost=1, trend=-0.2),
               stats("pricey", inv=10, cost=50, trend=0.8)], cfg)
    assert sorted(s, key=view_sort_key("used", cfg))[0].skill_name == "cheap"
    assert sorted(s, key=view_sort_key("expensive", cfg))[0].skill_name == "pricey"
    assert sorted(s, key=view_sort_key("growing", cfg))[0].skill_name == "pricey"
