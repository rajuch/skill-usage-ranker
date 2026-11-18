"""Compute-once behaviour: each day is fetched from the source at most once."""

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import SALT, SYNTH_DAYS, SYNTH_END

import skill_ranker
from skill_ranker.adapters import OtlpJsonlAdapter
from skill_ranker.cache import Cache, SaltMismatchError
from skill_ranker.config import Config
from skill_ranker.engine import Engine
from skill_ranker.window import parse_window


class CountingAdapter(OtlpJsonlAdapter):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fetched: list[date] = []

    def fetch_day(self, day):
        self.fetched.append(day)
        return super().fetch_day(day)


AFTER = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)  # all synthetic days complete


@pytest.fixture
def env(tmp_path, synthetic_dir):
    cfg = Config()
    cfg.cache.path = tmp_path / "cache.db"
    cache = Cache(cfg.cache.path)
    cache.check_salt(SALT)
    adapter = CountingAdapter([synthetic_dir], SALT)
    yield cfg, cache, adapter
    cache.close()


def window(days=SYNTH_DAYS):
    return parse_window(start=(SYNTH_END - timedelta(days=days - 1)).isoformat(), end=SYNTH_END.isoformat())


def test_second_run_uses_cache_only(env):
    cfg, cache, adapter = env
    first = Engine(cfg, adapter, cache, now=AFTER).rank(window())
    assert len(adapter.fetched) == SYNTH_DAYS
    assert len(first.info.days_computed) == SYNTH_DAYS

    second = Engine(cfg, adapter, cache, now=AFTER).rank(window())
    assert len(adapter.fetched) == SYNTH_DAYS  # nothing new fetched
    assert second.info.ranking_from_cache
    assert [s.skill_name for s in second.ranked] == [s.skill_name for s in first.ranked]
    assert [round(s.score, 9) for s in second.ranked] == [round(s.score, 9) for s in first.ranked]


def test_overlapping_window_only_fetches_new_days(env):
    cfg, cache, adapter = env
    Engine(cfg, adapter, cache, now=AFTER).rank(window(7))
    assert len(adapter.fetched) == 7
    res = Engine(cfg, adapter, cache, now=AFTER).rank(window(14))
    assert len(adapter.fetched) == 14  # only the 7 older days were added
    assert len(res.info.days_from_cache) == 7 and len(res.info.days_computed) == 7


def test_weight_change_reuses_day_partials(env):
    cfg, cache, adapter = env
    Engine(cfg, adapter, cache, now=AFTER).rank(window())
    cfg.ranking.weights.trend = 0.5
    res = Engine(cfg, adapter, cache, now=AFTER).rank(window())
    assert not res.info.ranking_from_cache  # rankings recomputed...
    assert len(adapter.fetched) == SYNTH_DAYS  # ...from cached days, no refetch


def test_incomplete_day_cached_with_ttl(env):
    cfg, cache, adapter = env
    last = SYNTH_END
    w = parse_window(start=last.isoformat(), end=last.isoformat())
    during = datetime(last.year, last.month, last.day, 15, 0, tzinfo=UTC)

    r1 = Engine(cfg, adapter, cache, now=during).rank(w)
    assert r1.info.incomplete_days == [last]
    Engine(cfg, adapter, cache, now=during + timedelta(minutes=5)).rank(w)
    assert adapter.fetched == [last]  # within TTL
    Engine(cfg, adapter, cache, now=during + timedelta(minutes=20)).rank(w)
    assert adapter.fetched == [last, last]  # TTL expired
    # once the day settles it is recomputed one final time, then never again
    final = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1, hours=3)
    Engine(cfg, adapter, cache, now=final).rank(w)
    Engine(cfg, adapter, cache, now=final + timedelta(days=5)).rank(w)
    assert adapter.fetched == [last, last, last]


def test_refresh_recomputes(env):
    cfg, cache, adapter = env
    Engine(cfg, adapter, cache, now=AFTER).rank(window(3))
    Engine(cfg, adapter, cache, now=AFTER).rank(window(3), refresh=True)
    assert len(adapter.fetched) == 6


def test_schema_version_bump_recomputes(env, monkeypatch):
    cfg, cache, adapter = env
    Engine(cfg, adapter, cache, now=AFTER).rank(window(3))
    monkeypatch.setattr(skill_ranker, "SCHEMA_VERSION", skill_ranker.SCHEMA_VERSION + 1)
    monkeypatch.setattr("skill_ranker.engine.SCHEMA_VERSION", skill_ranker.SCHEMA_VERSION)
    monkeypatch.setattr("skill_ranker.cache.SCHEMA_VERSION", skill_ranker.SCHEMA_VERSION)
    Engine(cfg, adapter, cache, now=AFTER).rank(window(3))
    assert len(adapter.fetched) == 6


def test_cached_partial_roundtrip_is_lossless(env):
    cfg, cache, adapter = env
    eng = Engine(cfg, adapter, cache, now=AFTER)
    fresh, _ = eng.load_partials(window(5))
    reloaded = [cache.get_day(adapter.source_id, p.day) for p in fresh]
    assert fresh == reloaded


def test_salt_change_is_refused(env):
    _, cache, _ = env
    with pytest.raises(SaltMismatchError):
        cache.check_salt("a-different-salt")


def test_no_raw_identifiers_in_cache(env, synthetic_dir):
    cfg, cache, adapter = env
    Engine(cfg, adapter, cache, now=AFTER).rank(window())
    blob = cfg.cache.path.read_bytes()
    sample = next(iter(synthetic_dir.glob("*.jsonl"))).read_text()
    import re
    uuids = set(re.findall(r'"user.account_uuid", "value": \{"stringValue": "([0-9a-f-]{36})"', sample))
    assert uuids and not any(u.encode() in blob for u in uuids)
