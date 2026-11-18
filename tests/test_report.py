import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import SALT, SYNTH_DAYS, SYNTH_END, gen_synthetic
from typer.testing import CliRunner

from skill_ranker.adapters import OtlpJsonlAdapter
from skill_ranker.cache import Cache
from skill_ranker.cli import app
from skill_ranker.config import Config
from skill_ranker.engine import Engine
from skill_ranker.report.build import build_report
from skill_ranker.report.render import compact, render_html, to_csv, to_json
from skill_ranker.window import parse_window

AFTER = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
WINDOW = parse_window(start=(SYNTH_END - timedelta(days=SYNTH_DAYS - 1)).isoformat(),
                      end=SYNTH_END.isoformat())


@pytest.fixture
def report(tmp_path, synthetic_dir):
    with Cache(tmp_path / "c.db") as cache:
        cache.check_salt(SALT)
        eng = Engine(Config(), OtlpJsonlAdapter([synthetic_dir], SALT), cache, now=AFTER)
        yield build_report(eng, WINDOW)


def test_summary_is_consistent(report):
    s = report.summary
    assert s.invocations == sum(p.invocations for p in report.daily)
    assert 0 < s.skill_users <= s.active_users
    assert s.adoption == pytest.approx(s.skill_users / s.active_users)
    assert 0 < s.skill_cost < s.total_cost  # generator includes prompts without skills
    assert s.prompts > s.invocations


def test_leaderboard_and_sparklines(report):
    assert [r.rank for r in report.leaderboard] == list(range(1, len(report.leaderboard) + 1))
    for r in report.leaderboard:
        assert len(r.spark) == len(report.days)
        assert sum(r.spark) == r.stats.invocations  # all days complete here


def test_small_teams_are_folded(tmp_path):
    out = tmp_path / "data"
    gen_synthetic.generate(days=3, end=SYNTH_END, users=7, prompts_per_day=200, seed=3,
                           redact=False, out=out)  # 7 users over 5 teams -> some teams have 1
    cfg = Config()
    cfg.ranking.min_team_users = 2
    with Cache(tmp_path / "c.db") as cache:
        cache.check_salt(SALT)
        r = build_report(Engine(cfg, OtlpJsonlAdapter([out], SALT), cache, now=AFTER),
                         parse_window(start=(SYNTH_END - timedelta(days=2)).isoformat(),
                                      end=SYNTH_END.isoformat()))
    assert r.quality.hidden_teams > 0
    assert all(t.active_users >= 2 for t in r.teams)


def test_html_is_self_contained_and_escapes_names(report):
    evil = '<img src=x onerror=alert(1)>"\'&'
    report.leaderboard[0].stats.skill_name = evil
    html = render_html(report)
    assert evil not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    # no external requests: no http(s) URLs in src/href, no @import
    assert not re.search(r'(src|href)\s*=\s*"https?://', html)
    assert "@import" not in html
    assert "<title>Skill Usage Report</title>" in html


def test_csv_neutralizes_formula_injection(report):
    report.leaderboard[0].stats.skill_name = "=HYPERLINK(\"http://x\")"
    rows = list(csv.DictReader(io.StringIO(to_csv(report))))
    assert rows[0]["skill_name"].startswith("'=")
    assert len(rows) == len(report.leaderboard)


def test_json_export_is_valid(report):
    d = json.loads(to_json(report))
    assert d["skills"] and d["summary"]["invocations"] == report.summary.invocations
    assert {"rank", "skill_name", "score", "success_rate"} <= set(d["skills"][0])


@pytest.mark.parametrize("n, expected", [(0, "0"), (1284, "1,284"), (9999, "9,999"),
                                          (12900, "12.9K"), (4_200_000, "4.2M"), (10_000, "10K")])
def test_compact(n, expected):
    assert compact(n) == expected


def test_cli_report_and_team_privacy(tmp_path, synthetic_dir):
    env = {"SKILL_RANKER_SALT": SALT, "SKILL_RANKER_CACHE": str(tmp_path / "c.db")}
    runner = CliRunner()
    out = tmp_path / "r.html"
    res = runner.invoke(app, ["report", "-i", str(synthetic_dir), "--from", WINDOW.start.isoformat(),
                              "--to", WINDOW.end.isoformat(), "-o", str(out)], env=env)
    assert res.exit_code == 0, res.output
    assert out.read_text().startswith("<!doctype html>")
    res = runner.invoke(app, ["report", "-i", str(synthetic_dir), "--team", "no-such-team",
                              "-o", str(tmp_path / "t.html")], env=env)
    assert res.exit_code == 2 and "cannot be identified" in res.output
    res = runner.invoke(app, ["export", "-i", str(synthetic_dir), "--from", WINDOW.start.isoformat(),
                              "--to", WINDOW.end.isoformat(), "-f", "json"], env=env)
    assert res.exit_code == 0 and json.loads(res.output)["skills"]
