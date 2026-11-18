"""Local SQLite cache of per-day partial aggregates and final rankings.

Raw events are never stored. Each completed UTC day is computed once per source and kept;
the current (incomplete) day is cached with a short TTL.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from . import SCHEMA_VERSION
from .models import DayPartial, SkillDayRow, TeamDayTotals

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS day_status (
  source TEXT NOT NULL, day TEXT NOT NULL,
  computed_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
  is_complete INTEGER NOT NULL, unresolved_names INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, day)
);

CREATE TABLE IF NOT EXISTS skill_day (
  source TEXT NOT NULL, day TEXT NOT NULL, org_id TEXT NOT NULL, team TEXT NOT NULL,
  skill_name TEXT NOT NULL, skill_source TEXT NOT NULL,
  invocations INTEGER NOT NULL, slash_invocations INTEGER NOT NULL,
  successes INTEGER NOT NULL, failures INTEGER NOT NULL,
  accepts INTEGER NOT NULL, rejects INTEGER NOT NULL,
  cost_usd REAL NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL, duration_hist TEXT NOT NULL,
  PRIMARY KEY (source, day, org_id, team, skill_name)
);

CREATE TABLE IF NOT EXISTS skill_day_user (
  source TEXT NOT NULL, day TEXT NOT NULL, org_id TEXT NOT NULL, team TEXT NOT NULL,
  skill_name TEXT NOT NULL, user_key TEXT NOT NULL, invocations INTEGER NOT NULL,
  PRIMARY KEY (source, day, org_id, team, skill_name, user_key)
);

CREATE TABLE IF NOT EXISTS skill_day_error (
  source TEXT NOT NULL, day TEXT NOT NULL, org_id TEXT NOT NULL, team TEXT NOT NULL,
  skill_name TEXT NOT NULL, error_type TEXT NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (source, day, org_id, team, skill_name, error_type)
);

CREATE TABLE IF NOT EXISTS team_day (
  source TEXT NOT NULL, day TEXT NOT NULL, org_id TEXT NOT NULL, team TEXT NOT NULL,
  prompts INTEGER NOT NULL, cost_usd REAL NOT NULL,
  PRIMARY KEY (source, day, org_id, team)
);

CREATE TABLE IF NOT EXISTS active_user_day (
  source TEXT NOT NULL, day TEXT NOT NULL, org_id TEXT NOT NULL, team TEXT NOT NULL,
  user_key TEXT NOT NULL, prompts INTEGER NOT NULL,
  PRIMARY KEY (source, day, org_id, team, user_key)
);

CREATE TABLE IF NOT EXISTS ranking_cache (
  cache_key TEXT PRIMARY KEY, source TEXT NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


DAY_TABLES = ("skill_day", "skill_day_user", "skill_day_error", "team_day", "active_user_day",
              "day_status")


@dataclass(slots=True)
class DayStatus:
    day: date
    computed_at: datetime
    schema_version: int
    is_complete: bool
    unresolved_names: int


class SaltMismatchError(RuntimeError):
    pass


class Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path)
        if new:
            os.chmod(self.path, 0o600)
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ salt guard

    def check_salt(self, salt: str) -> None:
        """User keys are salted hashes; mixing salts would split users. Refuse silently wrong data."""
        fp = hashlib.sha256(f"skill-ranker:{salt}".encode()).hexdigest()[:16]
        row = self.db.execute("SELECT value FROM meta WHERE key='salt_fp'").fetchone()
        if row is None:
            with self.db:
                self.db.execute("INSERT INTO meta VALUES ('salt_fp', ?)", (fp,))
        elif row[0] != fp:
            raise SaltMismatchError(
                "The user-hash salt differs from the one this cache was built with. "
                "Restore the original salt or run `skill-ranker cache clear`."
            )

    # ------------------------------------------------------------ day partials

    def day_status(self, source: str, day: date) -> DayStatus | None:
        row = self.db.execute(
            "SELECT computed_at, schema_version, is_complete, unresolved_names "
            "FROM day_status WHERE source=? AND day=?", (source, day.isoformat())).fetchone()
        if row is None:
            return None
        return DayStatus(day, datetime.fromisoformat(row[0]), row[1], bool(row[2]), row[3])

    def list_status(self, source: str | None = None) -> list[tuple[str, DayStatus]]:
        q = "SELECT source, day, computed_at, schema_version, is_complete, unresolved_names FROM day_status"
        args: tuple = ()
        if source:
            q += " WHERE source=?"
            args = (source,)
        return [(s, DayStatus(date.fromisoformat(d), datetime.fromisoformat(c), v, bool(i), u))
                for s, d, c, v, i, u in self.db.execute(q + " ORDER BY source, day", args)]

    def put_day(self, source: str, part: DayPartial, *, is_complete: bool,
                computed_at: datetime | None = None) -> None:
        d = part.day.isoformat()
        now = (computed_at or datetime.now(UTC)).isoformat()
        with self.db:
            self._delete_day(source, d)
            self.db.executemany(
                "INSERT INTO skill_day VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(source, d, r.org_id, r.team, r.skill_name, r.skill_source, r.invocations,
                  r.slash_invocations, r.successes, r.failures, r.accepts, r.rejects, r.cost_usd,
                  r.input_tokens, r.output_tokens, r.cache_read_tokens, json.dumps(r.duration_hist))
                 for r in part.skills.values()])
            self.db.executemany(
                "INSERT INTO skill_day_user VALUES (?,?,?,?,?,?,?)",
                [(source, d, o, t, s, u, n) for (o, t, s, u), n in part.users.items()])
            self.db.executemany(
                "INSERT INTO skill_day_error VALUES (?,?,?,?,?,?,?)",
                [(source, d, o, t, s, e, n) for (o, t, s, e), n in part.errors.items()])
            self.db.executemany(
                "INSERT INTO team_day VALUES (?,?,?,?,?,?)",
                [(source, d, o, t, v.prompts, v.cost_usd) for (o, t), v in part.team_totals.items()])
            self.db.executemany(
                "INSERT INTO active_user_day VALUES (?,?,?,?,?,?)",
                [(source, d, o, t, u, n) for (o, t, u), n in part.active_users.items()])
            self.db.execute(
                "INSERT INTO day_status VALUES (?,?,?,?,?,?)",
                (source, d, now, SCHEMA_VERSION, int(is_complete), part.unresolved_names))

    def get_day(self, source: str, day: date) -> DayPartial:
        d = day.isoformat()
        part = DayPartial(day=day)
        for row in self.db.execute(
                "SELECT org_id, team, skill_name, skill_source, invocations, slash_invocations, "
                "successes, failures, accepts, rejects, cost_usd, input_tokens, output_tokens, "
                "cache_read_tokens, duration_hist FROM skill_day WHERE source=? AND day=?", (source, d)):
            r = SkillDayRow(*row[:14], duration_hist=json.loads(row[14]))
            part.skills[(r.org_id, r.team, r.skill_name)] = r
        for o, t, s, u, n in self.db.execute(
                "SELECT org_id, team, skill_name, user_key, invocations FROM skill_day_user "
                "WHERE source=? AND day=?", (source, d)):
            part.users[(o, t, s, u)] = n
        for o, t, s, e, n in self.db.execute(
                "SELECT org_id, team, skill_name, error_type, n FROM skill_day_error "
                "WHERE source=? AND day=?", (source, d)):
            part.errors[(o, t, s, e)] = n
        for o, t, p, c in self.db.execute(
                "SELECT org_id, team, prompts, cost_usd FROM team_day WHERE source=? AND day=?", (source, d)):
            part.team_totals[(o, t)] = TeamDayTotals(p, c)
        for o, t, u, n in self.db.execute(
                "SELECT org_id, team, user_key, prompts FROM active_user_day WHERE source=? AND day=?",
                (source, d)):
            part.active_users[(o, t, u)] = n
        st = self.day_status(source, day)
        part.unresolved_names = st.unresolved_names if st else 0
        return part

    def _delete_day(self, source: str, d: str) -> None:
        for table in DAY_TABLES:
            self.db.execute(f"DELETE FROM {table} WHERE source=? AND day=?", (source, d))

    def invalidate_days(self, source: str, start: date, end: date) -> int:
        with self.db:
            n = self.db.execute("SELECT count(*) FROM day_status WHERE source=? AND day BETWEEN ? AND ?",
                                (source, start.isoformat(), end.isoformat())).fetchone()[0]
            for table in DAY_TABLES:
                self.db.execute(f"DELETE FROM {table} WHERE source=? AND day BETWEEN ? AND ?",
                                (source, start.isoformat(), end.isoformat()))
            self.db.execute(
                "DELETE FROM ranking_cache WHERE source=? AND window_end >= ? AND window_start <= ?",
                (source, start.isoformat(), end.isoformat()))
        return n

    # ------------------------------------------------------------ final rankings

    def get_ranking(self, key: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM ranking_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_ranking(self, key: str, source: str, start: date, end: date, payload: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ranking_cache VALUES (?,?,?,?,?,?)",
                            (key, source, start.isoformat(), end.isoformat(),
                             datetime.now(UTC).isoformat(), json.dumps(payload)))

    def clear(self) -> None:
        with self.db:
            for table in (*DAY_TABLES, "ranking_cache", "meta"):
                self.db.execute(f"DELETE FROM {table}")
