"""Source adapter contract. Every telemetry backend implements this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..models import DayPartial, SkillEvent


@dataclass(slots=True)
class HealthReport:
    #: (name, status, detail) with status one of "ok", "warn", "fail"
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(status != "fail" for _, status, _ in self.checks)


class SourceAdapter(Protocol):
    #: Stable identifier of the data source (type + location). Cache rows are keyed by it,
    #: so two different backends never share cached days.
    source_id: str

    def healthcheck(self) -> HealthReport: ...

    def fetch_day(self, day: date) -> list[SkillEvent]:
        """Return normalized events whose timestamp falls on `day` (UTC)."""
        ...

    def aggregate_day(self, day: date) -> DayPartial | None:
        """Optional fast path: let the backend aggregate. Return None to fall back to fetch_day."""
        ...
