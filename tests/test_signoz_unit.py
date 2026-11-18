"""SigNoz adapter tests that need no live server."""

from datetime import date

import pytest

from skill_ranker.adapters import build_adapter
from skill_ranker.adapters.signoz import SignozAdapter, SignozError, _lit, _queries
from skill_ranker.config import Config
from skill_ranker.normalize import hash_user

DAY = date(2026, 11, 16)


@pytest.mark.parametrize("bad", ["x'; DROP TABLE t; --", "a b", "claude-code'", ""])
def test_sql_literal_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        _lit(bad)


def test_unsafe_service_name_rejected_at_construction():
    with pytest.raises(ValueError):
        SignozAdapter("http://x", "key", "salt", service_name="x' OR 1=1 --")


def test_queries_are_bounded_to_one_utc_day():
    q = _queries(DAY, "claude-code")
    assert set(q) == {"skills", "hist", "users", "errors", "unresolved", "team_totals", "active_users"}
    for sql in q.values():
        assert "timestamp >= 1794787200000000000 AND timestamp < 1794873600000000000" in sql
        assert "resources_string['service.name'] = 'claude-code'" in sql


def test_aggregate_day_parses_response_and_hashes_users_locally(monkeypatch):
    a = SignozAdapter("http://signoz", "key", "salt")
    canned = {
        "skills": [{"org": "o", "team": "payments", "skill": "code-review", "skill_source": "bundled",
                    "invocations": 3, "slash_invocations": 1, "successes": 2, "failures": 0,
                    "accepts": 2, "rejects": 0, "cost_nano_usd": 123456789, "input_tokens": 10,
                    "output_tokens": 20, "cache_read_tokens": 30}],
        "hist": [{"org": "o", "team": "payments", "skill": "code-review", "bucket": 2, "n": 2}],
        "users": [{"org": "o", "team": "payments", "skill": "code-review", "user_raw": "uuid-1", "n": 3}],
        "errors": [],
        "unresolved": [{"unresolved_n": 4}],
        "team_totals": [{"org": "o", "team": "payments", "prompts": 9, "cost_nano_usd": 2_500_000_000}],
        "active_users": [{"org": "o", "team": "payments", "user_raw": "uuid-1", "n": 9}],
    }
    sent = {}

    def fake_query(queries):
        sent.update(queries)
        return canned

    monkeypatch.setattr(a, "query", fake_query)
    part = a.aggregate_day(DAY)
    row = part.skills[("o", "payments", "code-review")]
    assert row.invocations == 3 and row.cost_usd == pytest.approx(0.123456789)
    assert row.duration_hist[2] == 2
    assert part.users == {("o", "payments", "code-review", hash_user("uuid-1", "salt")): 3}
    assert part.unresolved_names == 4
    assert part.team_totals[("o", "payments")].prompts == 9
    assert part.team_totals[("o", "payments")].cost_usd == pytest.approx(2.5)
    assert part.active_users == {("o", "payments", hash_user("uuid-1", "salt")): 9}
    assert all("salt" not in sql for sql in sent.values())  # salt never leaves the client


def test_unreachable_server_raises_clear_error():
    a = SignozAdapter("http://127.0.0.1:9", "key", "salt", timeout_s=2)
    with pytest.raises(SignozError, match="cannot reach SigNoz"):
        a.query({"A": "SELECT 1"})


def test_missing_api_key_is_a_config_error(monkeypatch):
    monkeypatch.setenv("SKILL_RANKER_SALT", "s")
    monkeypatch.delenv("SIGNOZ_API_KEY", raising=False)
    cfg = Config()
    cfg.source.type = "signoz"
    with pytest.raises(ValueError, match="SIGNOZ_API_KEY"):
        build_adapter(cfg)
