"""Time-window planning: split a window into UTC day buckets and decide cacheability."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta


@dataclass(frozen=True, slots=True)
class Window:
    start: date  # inclusive
    end: date  # inclusive

    @property
    def days(self) -> list[date]:
        n = (self.end - self.start).days + 1
        return [self.start + timedelta(days=i) for i in range(n)]

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def parse_window(spec: str | None = None, start: str | None = None, end: str | None = None,
                 now: datetime | None = None) -> Window:
    """Build a window from `"30d"`-style specs or explicit ISO dates. Days are UTC.

    A relative spec ends today (UTC) and includes it, e.g. "7d" = today + 6 previous days.
    """
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    if start or end:
        s = date.fromisoformat(start) if start else None
        e = date.fromisoformat(end) if end else today
        if s is None:
            raise ValueError("--from is required when --to is given")
        if s > e:
            raise ValueError(f"window start {s} is after end {e}")
        return Window(s, e)
    m = re.fullmatch(r"\s*(\d+)\s*d\s*", spec or "30d")
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"invalid window {spec!r}; use e.g. 7d, 30d, 90d")
    n = int(m.group(1))
    return Window(today - timedelta(days=n - 1), today)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def is_day_complete(day: date, now: datetime, settle: timedelta) -> bool:
    """A day is final once `settle` has passed after its UTC midnight end (late data)."""
    _, end = day_bounds(day)
    return now >= end + settle
