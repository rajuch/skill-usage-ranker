"""Core data types shared by adapters, aggregation, cache and ranking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Upper bounds (ms) of the duration histogram buckets; the last bucket is open-ended.
DURATION_BUCKETS_MS: tuple[int, ...] = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)

REDACTED_SKILL_NAMES = frozenset({"", "custom_skill", "third-party"})


@dataclass(slots=True)
class SkillEvent:
    """One normalized telemetry record relevant to skill ranking.

    `kind` is one of:
      - "activation": a skill_activated event (the invocation counter)
      - "tool_result": the Skill tool finished (success / duration)
      - "decision": the Skill tool was permission-checked (accept / reject)
      - "api_cost": a model request made while a skill was active (cost / tokens)
      - "prompt": a user prompt (any, skill or not) - adoption denominator
      - "api_other": a model request with no active skill - total-cost denominator
    """

    kind: str
    ts: datetime
    skill_name: str
    prompt_id: str = ""
    session_id: str = ""
    user_key: str = ""
    org_id: str = ""
    team: str = "unknown"
    skill_source: str = ""
    trigger: str = ""
    success: bool | None = None
    decision: str = ""
    duration_ms: float | None = None
    error_type: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(slots=True)
class SkillDayRow:
    """Additive per-day counters for one (org, team, skill)."""

    org_id: str
    team: str
    skill_name: str
    skill_source: str = ""
    invocations: int = 0
    slash_invocations: int = 0
    successes: int = 0
    failures: int = 0
    accepts: int = 0
    rejects: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    duration_hist: list[int] = field(default_factory=lambda: [0] * (len(DURATION_BUCKETS_MS) + 1))


@dataclass(slots=True)
class TeamDayTotals:
    """All Claude Code activity for one (org, team) on one day, skill or not."""

    prompts: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class DayPartial:
    """Everything cached for one UTC day. Combinable across days without raw events."""

    day: date
    skills: dict[tuple[str, str, str], SkillDayRow] = field(default_factory=dict)
    # (org_id, team, skill_name, user_key) -> invocations
    users: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    # (org_id, team, skill_name, error_type) -> count
    errors: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    unresolved_names: int = 0  # activations whose skill name stayed redacted
    # (org_id, team) -> all prompts / all model cost (skill or not)
    team_totals: dict[tuple[str, str], TeamDayTotals] = field(default_factory=dict)
    # (org_id, team, user_key) -> prompts: everyone who used Claude Code that day
    active_users: dict[tuple[str, str, str], int] = field(default_factory=dict)


@dataclass(slots=True)
class SkillStats:
    """Window-level metrics for one skill (after merging day partials)."""

    skill_name: str
    skill_source: str
    invocations: int
    slash_invocations: int
    unique_users: int
    unique_teams: int
    active_days: int
    successes: int
    failures: int
    accepts: int
    rejects: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    p50_duration_ms: float | None
    p95_duration_ms: float | None
    retention: float | None  # share of previous-period users who used it again this period
    trend: float | None  # growth of recent period vs previous period
    top_errors: list[tuple[str, int]] = field(default_factory=list)
    score: float = 0.0
    retention_base: int = 0  # users in the previous period (retention denominator)
    prev_invocations: int = 0  # invocations in the previous period (trend denominator)

    @property
    def success_rate(self) -> float | None:
        n = self.successes + self.failures
        return self.successes / n if n else None

    @property
    def acceptance_rate(self) -> float | None:
        n = self.accepts + self.rejects
        return self.accepts / n if n else None

    @property
    def cost_per_invocation(self) -> float | None:
        return self.cost_usd / self.invocations if self.invocations else None
