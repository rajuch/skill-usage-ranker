"""Adapter for OTLP/JSON files: one ExportLogsServiceRequest per line.

Produced by an OTel Collector `file` exporter, by scripts/otlp_capture.py, or by
scripts/gen_synthetic.py. Used for local development and tests.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..models import DayPartial, SkillEvent
from ..normalize import RawRecord, iter_otlp_log_records, records_to_events
from .base import HealthReport


class OtlpJsonlAdapter:
    def __init__(self, paths: list[str | Path], salt: str, service_name: str = "claude-code"):
        self.inputs = sorted({Path(p).expanduser().resolve() for p in paths})
        self.paths = sorted({Path(p).expanduser().resolve() for p in self._expand(paths)})
        self.salt = salt
        self.service_name = service_name
        # identify the source by what the user pointed at (dirs stay dirs), not every file in it
        self.source_id = f"otlp-jsonl:{service_name}:" + ",".join(str(p) for p in self.inputs)
        self._by_day: dict[date, list[RawRecord]] | None = None

    @staticmethod
    def _expand(paths: list[str | Path]) -> list[Path]:
        out: list[Path] = []
        for p in map(Path, paths):
            p = p.expanduser()
            out.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
        return out

    def _load(self) -> dict[date, list[RawRecord]]:
        if self._by_day is None:
            self._by_day = {}
            for path in self.paths:
                with path.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        for rec in iter_otlp_log_records(json.loads(line)):
                            self._by_day.setdefault(rec.ts.date(), []).append(rec)
        return self._by_day

    def healthcheck(self) -> HealthReport:
        checks = []
        missing = [p for p in self.paths if not p.exists()]
        checks.append(("files exist", "ok" if not missing and self.paths else "fail",
                       f"missing: {missing}" if missing else f"{len(self.paths)} file(s)"))
        if not missing and self.paths:
            by_day = self._load()
            n = sum(len(v) for v in by_day.values())
            checks.append(("log records found", "ok" if n else "fail",
                           f"{n} records over {len(by_day)} day(s)"))
        return HealthReport(checks)

    def fetch_day(self, day: date) -> list[SkillEvent]:
        return records_to_events(self._load().get(day, []), self.salt, self.service_name)

    def aggregate_day(self, day: date) -> DayPartial | None:
        return None
