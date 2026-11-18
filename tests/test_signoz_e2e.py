"""End-to-end contract test against a live self-hosted SigNoz.

Ingests synthetic telemetry through the real OTLP collector (unique service.name per run, so
real data and previous runs never interfere), then checks that for every day:

    SigNoz SQL fast path  ==  SigNoz raw fetch + Python  ==  OTLP JSONL adapter

Run with:
    set -a; source deploy/signoz/.api-key.env; set +a
    SIGNOZ_E2E=1 .venv/bin/pytest -q tests/test_signoz_e2e.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import SALT, gen_synthetic

from skill_ranker.adapters import OtlpJsonlAdapter, SignozAdapter
from skill_ranker.aggregate import aggregate_day
from skill_ranker.cache import Cache
from skill_ranker.config import Config
from skill_ranker.engine import Engine
from skill_ranker.models import DayPartial
from skill_ranker.window import parse_window

pytestmark = pytest.mark.skipif(
    os.environ.get("SIGNOZ_E2E") != "1" or not os.environ.get("SIGNOZ_API_KEY"),
    reason="set SIGNOZ_E2E=1 and SIGNOZ_API_KEY to run against a live SigNoz",
)

URL = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
OTLP = os.environ.get("SIGNOZ_OTLP", "http://localhost:4318")
DAYS = 4
END = datetime.now(UTC).date() - timedelta(days=1)  # recent, inside default log retention


def _post_otlp(path):
    n = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        req = urllib.request.Request(OTLP + "/v1/logs", data=line.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200
        n += sum(len(sl["logRecords"]) for rl in json.loads(line)["resourceLogs"]
                 for sl in rl["scopeLogs"])
    return n


def _wait_for_rows(adapter: SignozAdapter, expected: int, timeout_s: float = 90) -> None:
    sql = ("SELECT count() AS n FROM signoz_logs.distributed_logs_v2 "
           f"WHERE resources_string['service.name'] = '{adapter.service_name}'")
    deadline = time.monotonic() + timeout_s
    n = -1
    while time.monotonic() < deadline:
        n = int(adapter.query({"A": sql})["A"][0]["n"])
        if n >= expected:
            return
        time.sleep(2)
    pytest.fail(f"SigNoz ingested {n}/{expected} records for {adapter.service_name}")


def _comparable(p: DayPartial) -> dict:
    return {
        "skills": {k: (r.skill_source, r.invocations, r.slash_invocations, r.successes, r.failures,
                       r.accepts, r.rejects, round(r.cost_usd, 6), r.input_tokens, r.output_tokens,
                       r.cache_read_tokens, tuple(r.duration_hist))
                   for k, r in p.skills.items()},
        "users": dict(p.users),
        "errors": dict(p.errors),
        "unresolved": p.unresolved_names,
        "team_totals": {k: (v.prompts, round(v.cost_usd, 6)) for k, v in p.team_totals.items()},
        "active_users": dict(p.active_users),
    }


@pytest.fixture(scope="module", params=[False, True], ids=["tool-details-on", "redacted"])
def ingested(request, tmp_path_factory):
    redact = request.param
    service = f"claude-code-e2e-{uuid.uuid4().hex[:8]}"
    out = tmp_path_factory.mktemp(service)
    gen_synthetic.generate(days=DAYS, end=END, users=15, prompts_per_day=40, seed=5,
                           redact=redact, out=out, service_name=service)
    signoz = SignozAdapter(URL, os.environ["SIGNOZ_API_KEY"], SALT, service_name=service,
                           otlp_endpoint=OTLP)
    total = sum(_post_otlp(f) for f in sorted(out.glob("*.jsonl")))
    _wait_for_rows(signoz, total)
    return signoz, OtlpJsonlAdapter([out], SALT, service)


def test_sql_fast_path_matches_python_paths(ingested):
    signoz, jsonl = ingested
    for i in range(DAYS):
        day = END - timedelta(days=i)
        via_sql = _comparable(signoz.aggregate_day(day))
        via_fetch = _comparable(aggregate_day(day, signoz.fetch_day(day)))
        via_jsonl = _comparable(aggregate_day(day, jsonl.fetch_day(day)))
        assert via_sql["skills"], f"no data for {day}"
        assert via_sql == via_fetch, f"SQL vs fetch mismatch on {day}"
        assert via_fetch == via_jsonl, f"SigNoz vs JSONL mismatch on {day}"


def test_engine_rankings_match_across_sources(ingested, tmp_path):
    signoz, jsonl = ingested
    cfg = Config()
    w = parse_window(start=(END - timedelta(days=DAYS - 1)).isoformat(), end=END.isoformat())
    now = datetime.now(UTC) + timedelta(days=1)
    results = []
    for name, adapter in (("signoz", signoz), ("jsonl", jsonl)):
        with Cache(tmp_path / f"{name}.db") as cache:
            cache.check_salt(SALT)
            r = Engine(cfg, adapter, cache, now=now).rank(w)
            results.append([(s.skill_name, s.invocations, s.unique_users, round(s.score, 6))
                            for s in r.ranked])
    assert results[0] and results[0] == results[1]


def test_doctor_reports_healthy(ingested):
    signoz, _ = ingested
    report = signoz.healthcheck()
    statuses = {name: status for name, status, _ in report.checks}
    assert report.ok, report.checks
    assert statuses["SigNoz reachable"] == statuses["API key accepted"] == "ok"
    assert statuses["OTLP receiver accepting"] == "ok"
    assert statuses["skill telemetry (7d)"] == "ok"
