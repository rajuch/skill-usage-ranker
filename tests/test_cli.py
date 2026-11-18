import json

from conftest import SALT, SYNTH_END
from typer.testing import CliRunner

from skill_ranker.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {"SKILL_RANKER_SALT": SALT, "SKILL_RANKER_CACHE": str(tmp_path / "c.db")}


def test_rank_json(tmp_path, synthetic_dir):
    args = ["rank", "-i", str(synthetic_dir), "--from", "2026-10-21", "--to", SYNTH_END.isoformat(),
            "--json", "--top", "5"]
    res = runner.invoke(app, args, env=_env(tmp_path))
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert len(out["skills"]) == 5
    assert out["skills"][0]["skill_name"] == "code-review"
    assert "rarely-used" not in {s["skill_name"] for s in out["skills"]}


def test_rank_table_and_cache_status(tmp_path, synthetic_dir):
    env = _env(tmp_path)
    res = runner.invoke(app, ["rank", "-i", str(synthetic_dir), "--from", "2026-11-04",
                              "--to", SYNTH_END.isoformat(), "--view", "growing"], env=env)
    assert res.exit_code == 0, res.output
    assert "Skill ranking" in res.output and "7 computed" in res.output
    res = runner.invoke(app, ["rank", "-i", str(synthetic_dir), "--from", "2026-11-04",
                              "--to", SYNTH_END.isoformat()], env=env)
    assert "ranking served from cache" in res.output


def test_missing_salt_is_an_error(tmp_path, synthetic_dir):
    res = runner.invoke(app, ["rank", "-i", str(synthetic_dir)],
                        env={"SKILL_RANKER_CACHE": str(tmp_path / "c.db"), "SKILL_RANKER_SALT": ""})
    assert res.exit_code == 2 and "SKILL_RANKER_SALT" in res.output


def test_doctor(tmp_path, synthetic_dir):
    res = runner.invoke(app, ["doctor", "-i", str(synthetic_dir)], env=_env(tmp_path))
    assert res.exit_code == 0, res.output
    assert "log records found" in res.output
