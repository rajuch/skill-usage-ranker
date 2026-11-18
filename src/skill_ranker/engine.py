"""Compute-once orchestration: window -> cached/fresh day partials -> scored skill stats."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

from . import SCHEMA_VERSION
from .adapters.base import SourceAdapter
from .aggregate import aggregate_day, merge_partials
from .cache import Cache
from .config import Config
from .models import DayPartial, SkillStats
from .ranking import eligible, score
from .window import Window, is_day_complete


@dataclass(slots=True)
class RunInfo:
    window: Window
    days_from_cache: list[date] = field(default_factory=list)
    days_computed: list[date] = field(default_factory=list)
    incomplete_days: list[date] = field(default_factory=list)
    ranking_from_cache: bool = False
    unresolved_names: int = 0
    below_threshold: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RankResult:
    ranked: list[SkillStats]
    info: RunInfo


class Engine:
    def __init__(self, cfg: Config, adapter: SourceAdapter, cache: Cache,
                 now: datetime | None = None):
        self.cfg, self.adapter, self.cache = cfg, adapter, cache
        self._now = now

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _fresh(self, day: date) -> bool:
        """Is the cached copy of `day` usable as-is?"""
        st = self.cache.day_status(self.adapter.source_id, day)
        if st is None or st.schema_version != SCHEMA_VERSION:
            return False
        if st.is_complete:
            return True
        return self.now - st.computed_at < self.cfg.cache.partial_ttl

    def load_partials(self, window: Window, *, refresh: bool = False) -> tuple[list[DayPartial], RunInfo]:
        info = RunInfo(window=window)
        src = self.adapter.source_id
        if refresh:
            self.cache.invalidate_days(src, window.start, window.end)
        partials = []
        for day in window.days:
            if day > self.now.date():
                continue  # future days have no data
            complete = is_day_complete(day, self.now, self.cfg.cache.settle)
            if not complete:
                info.incomplete_days.append(day)
            if self._fresh(day):
                part = self.cache.get_day(src, day)
                info.days_from_cache.append(day)
            else:
                part = self.adapter.aggregate_day(day)
                if part is None:
                    part = aggregate_day(day, self.adapter.fetch_day(day))
                self.cache.put_day(src, part, is_complete=complete, computed_at=self.now)
                info.days_computed.append(day)
            info.unresolved_names += part.unresolved_names
            partials.append(part)
        return partials, info

    def _ranking_key(self, window: Window, team: str | None, org_id: str | None) -> str:
        blob = json.dumps([self.adapter.source_id, str(window), team, org_id,
                           self.cfg.ranking_fingerprint(), SCHEMA_VERSION])
        return hashlib.sha256(blob.encode()).hexdigest()

    def rank(self, window: Window, *, team: str | None = None, org_id: str | None = None,
             refresh: bool = False) -> RankResult:
        key = self._ranking_key(window, team, org_id)
        all_complete = all(is_day_complete(d, self.now, self.cfg.cache.settle) for d in window.days)
        if all_complete and not refresh and (cached := self.cache.get_ranking(key)) is not None:
            info = RunInfo(window=window, days_from_cache=list(window.days), ranking_from_cache=True)
            payload = cached
            ranked = [_stats_from_dict(d) for d in payload["ranked"]]
            info.below_threshold = payload["below_threshold"]
            info.unresolved_names = payload["unresolved_names"]
            return RankResult(ranked, info)

        partials, info = self.load_partials(window, refresh=refresh)
        stats = merge_partials(partials, team=team, org_id=org_id,
                               incomplete_days=frozenset(info.incomplete_days))
        keep, low = eligible(stats, self.cfg.ranking)
        ranked = score(keep, self.cfg.ranking)
        info.below_threshold = sorted(s.skill_name for s in low)
        if all_complete:
            self.cache.put_ranking(key, self.adapter.source_id, window.start, window.end, {
                "ranked": [asdict(s) for s in ranked],
                "below_threshold": info.below_threshold,
                "unresolved_names": info.unresolved_names,
            })
        return RankResult(ranked, info)


def _stats_from_dict(d: dict) -> SkillStats:
    d = dict(d)
    d["top_errors"] = [tuple(x) for x in d.get("top_errors", [])]
    return SkillStats(**d)
