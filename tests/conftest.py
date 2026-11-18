from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SALT = "test-salt"

_spec = importlib.util.spec_from_file_location("gen_synthetic", ROOT / "scripts" / "gen_synthetic.py")
gen_synthetic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_synthetic)

SYNTH_END = date(2026, 11, 10)
SYNTH_DAYS = 21


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    gen_synthetic.generate(days=SYNTH_DAYS, end=SYNTH_END, users=30, prompts_per_day=60,
                           seed=11, redact=False, out=out)
    return out


@pytest.fixture(scope="session")
def synthetic_redacted_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synthetic_redacted")
    gen_synthetic.generate(days=7, end=SYNTH_END, users=20, prompts_per_day=150,
                           seed=11, redact=True, out=out)
    return out


def load_records(path: Path):
    from skill_ranker.normalize import iter_otlp_log_records

    recs = []
    for line in path.read_text().splitlines():
        if line.strip():
            recs.extend(iter_otlp_log_records(json.loads(line)))
    return recs
