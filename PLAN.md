# Skill Usage Ranker — Project Plan

Rank Claude Code **Skills** across an **organization** by usage, effectiveness, and cost, computed
from the OpenTelemetry telemetry Claude Code already emits.

- **v1 backend:** SigNoz. The adapter layer is built so other platforms (Loki/Prometheus, Datadog,
  Honeycomb, …) can be added later without touching the ranking logic.
- **v1 output:** static HTML report (+ CSV/JSON export).
- **Storage:** no raw-event database. Each time window is computed **once** from SigNoz and the
  results are cached in a **local SQLite file**, so re-running a report never recomputes it.

---

## 1. Decisions

| Topic | Decision |
|---|---|
| Audience | **Organization** — platform/eng-enablement admins; org-wide view with team breakdowns |
| Backend | **SigNoz** in v1; pluggable adapters for later platforms |
| Storage | No ingestion DB. Compute per time window → cache results in local **SQLite** |
| Output | **Static HTML report** for v1 (no server, no dashboard) |
| Built-in vs custom skills | Ranked together, with a `skill_source` column and a filter/section per source *(default — change if needed)* |

**Non-goals (v1):** collecting telemetry ourselves, live dashboards, an API server, storing raw events,
LLM-judged skill quality.

---

## 2. Source Signals (what Claude Code emits)

Reference: https://code.claude.com/docs/en/monitoring-usage

> **M0 finding:** the primary signal is the `skill_activated` event (fires for both model-invoked and
> `/slash` skills, with `skill.name`, `invocation_trigger`, `skill.source`). Full verified mapping in
> [`docs/telemetry-mapping.md`](docs/telemetry-mapping.md).

| Signal | Type | Fields used | Requirement |
|---|---|---|---|
| `claude_code.skill_activated` | log event | `skill.name`, `invocation_trigger` (`claude-proactive` / `user-slash`), `skill.source` — **invocation counter** | custom names redacted to `custom_skill` unless `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool_result` | log event | `tool_name` (Skill tool), `tool_parameters.skill_name`, `success`, `duration_ms`, `error_type`, `tool_use_id` | `skill_name` only with `OTEL_LOG_TOOL_DETAILS=1` |
| `claude_code.tool_decision` | log event | `tool_name`, `decision` (accept/reject), `source`, `tool_parameters.skill_name` | same |
| `claude_code.user_prompt` | log event | `command_name`, `command_source` — `/skill` slash invocations | always |
| `claude_code.api_request` | log event | `skill.name`, `plugin.name`, `marketplace.name`, `cost_usd`, tokens, `model` | always |
| `claude_code.cost.usage` / `token.usage` | metrics | attr `skill.name` | always |

**Standard attributes** for grouping: `organization.id`, `user.account_uuid` / `user.id`, `session.id`,
`terminal.type`, `app.version`, `prompt.id`, plus custom resource attributes for teams.

**Caveats**
- Third-party plugin skills show `skill.name = "third-party"` on cost attribution → use
  `tool_parameters.skill_name` for the real name.
- `tool_result` is not emitted for rejected calls → rejections come from `tool_decision`.
- Subagents inherit `skill.name` → cost means "spent while skill active".
- Metrics use delta temporality by default → sum deltas within a window.

### Required org-wide client config
Push via **managed settings** so every developer emits consistently:
```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://<signoz-collector>:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_LOG_TOOL_DETAILS=1                                   # exact skill names
OTEL_RESOURCE_ATTRIBUTES=department=<dept>,team.id=<team> # team breakdowns
```

---

## 3. Architecture

```
Claude Code (org devs) ──OTLP──▶ OTel Collector ──▶ SigNoz (ClickHouse)
                                 (filter: keep only skill_name             ▲
                                  from tool_parameters)                    │ aggregate queries
                                                                           │ (per day bucket)
┌──────────────────────────────── skill-ranker ─────────────────────────────┴──────┐
│                                                                                   │
│  rank/report --window 30d                                                          │
│        │                                                                          │
│        ▼                                                                          │
│  Window Planner ── split window into UTC day buckets ──┐                          │
│        │                                                ▼                          │
│        │                               ┌──────────── SQLite cache ─────────────┐  │
│        │   cached day? ── yes ────────▶│ daily partial aggregates per skill     │  │
│        │        │ no                   │ final rankings per (window, config)    │  │
│        │        ▼                      └───────────────────────────────────────┘  │
│        │   Source Adapter (SigNoz) → Normalizer → Day Aggregator ──write──▶ ▲      │
│        ▼                                                                          │
│  Combine day buckets → Metrics → Ranking Engine → HTML / CSV / JSON               │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Compute-once flow
1. **Plan** — split the requested window (e.g. last 30 days) into **UTC day buckets**.
2. **Look up** — load each completed day from the SQLite cache.
3. **Fill gaps** — for missing days, query SigNoz for that day only, normalize events **in memory**,
   aggregate to per-skill partials, write to SQLite, discard the raw events.
4. **Combine** — merge day partials into window metrics; rank; render.
5. **Cache final result** — store the ranked output keyed by `(window, filters, config_hash)` so the same
   report renders instantly next time.

Result: a 30-day report queries SigNoz for 30 days the first time; the next day it queries **one** new day.

### 3.2 Cache rules
- **Completed days are immutable** → cached permanently.
- **Today (partial day)** → never cached; or cached with a short TTL (default 15 min) and recomputed after.
- **Late-arriving data** → configurable *settle period* (default 2h after midnight UTC) before a day is
  considered complete.
- **Invalidation** — every row stores `schema_version`; bumping it (or `--refresh`) recomputes.
  Weight changes only invalidate final rankings, not day partials (`config_hash`).

### 3.3 Making daily partials combinable
Some metrics don't add up across days, so the day partials store mergeable forms:

| Metric | Stored per day | Combined over window |
|---|---|---|
| invocations, successes, failures, accepts, rejects | counts | sum |
| cost_usd, tokens | sums | sum |
| unique users / sessions | per-day `(skill, user_key)` and `(skill, session_id)` rows with counts | `COUNT(DISTINCT)` over union |
| p50 / p95 duration | fixed-bucket duration histogram | merge histograms → percentile |
| retention, trend | derived from per-day user rows and counts | computed at combine time |
| error types | `(skill, error_type)` counts | sum |

Per-day user rows are small (skills × active users) and store only **salted hashes**, never emails.

### 3.4 SQLite schema (draft)

```sql
CREATE TABLE day_status (
  source TEXT, day DATE, org_id TEXT,
  computed_at TIMESTAMP, schema_version INT, is_complete INT,
  PRIMARY KEY (source, day, org_id)
);

CREATE TABLE skill_day (               -- additive counters
  source TEXT, day DATE, org_id TEXT, team TEXT,
  skill_name TEXT, skill_source TEXT, plugin_name TEXT,
  invocations INT, successes INT, failures INT,
  accepts INT, rejects INT, slash_invocations INT,
  cost_usd REAL, input_tokens INT, output_tokens INT, cache_read_tokens INT,
  duration_hist TEXT,                  -- JSON bucket counts
  PRIMARY KEY (source, day, org_id, team, skill_name)
);

CREATE TABLE skill_day_user (          -- for distinct users / retention
  source TEXT, day DATE, org_id TEXT, team TEXT,
  skill_name TEXT, user_key TEXT, invocations INT,
  PRIMARY KEY (source, day, org_id, team, skill_name, user_key)
);

CREATE TABLE skill_day_error (
  source TEXT, day DATE, org_id TEXT, skill_name TEXT, error_type TEXT, n INT,
  PRIMARY KEY (source, day, org_id, skill_name, error_type)
);

CREATE TABLE ranking_cache (           -- final results
  cache_key TEXT PRIMARY KEY,          -- hash(window, filters, config_hash, schema_version)
  window_start DATE, window_end DATE,
  created_at TIMESTAMP, payload TEXT   -- JSON ranked rows
);
```

Python's built-in `sqlite3` — no extra DB dependency or server. Single file: `~/.skill-ranker/cache.db`
(configurable).

### 3.5 Source adapter interface

```python
class SourceAdapter(Protocol):
    name: str
    def healthcheck(self) -> HealthReport: ...
    def fetch_day(self, day: date, org_id: str | None) -> Iterator[SkillEvent]: ...
    # optional fast path: let the backend aggregate
    def aggregate_day(self, day: date, org_id: str | None) -> DayPartial | None: ...
```

- **SigNoz adapter (v1):** prefer `aggregate_day` via SigNoz's ClickHouse-SQL query API so grouping by
  skill/user/team happens server-side (small payloads). Fall back to `fetch_day` (paged log query) +
  in-memory aggregation if SQL access isn't available. M0 confirms which works and the exact table/attribute paths.
- **Future adapters** (Loki/Prometheus, Datadog, Honeycomb, Elastic, OTLP JSONL) implement the same
  interface + a `mapping.yaml`. A shared contract test suite guarantees identical `DayPartial` output.
- **OTLP JSONL adapter** (reads Collector `file` exporter) is built in M1 purely for **tests and local dev**.

---

## 4. Ranking Model

### 4.1 Per-skill window metrics

| Metric | Definition |
|---|---|
| `invocations` | `skill_activated` events (split by `invocation_trigger`: model vs. slash) |
| `unique_users` | distinct `user_key` |
| `unique_teams` | distinct team — **org adoption breadth** |
| `success_rate` | successes / tool results |
| `acceptance_rate` | accepts / decisions |
| `p50/p95_duration_ms` | from merged histograms |
| `cost_usd`, `cost_per_invocation` | from `skill.name` attribution |
| `retention` | % users active in week N who use it again in week N+1 |
| `trend` | WoW growth + slope over last k weeks |

### 4.2 Composite score (weights in `ranker.yaml`)

```
score = w1·pct(log(1+invocations)) + w2·pct(unique_users) + w3·pct(unique_teams)
      + w4·success_rate* + w5·acceptance_rate* + w6·pct(retention) + w7·pct(trend)
      − w8·pct(cost_per_invocation)
```
- `pct` = percentile rank within the window; `*` = Bayesian-smoothed toward the org mean.
- Minimum support (default ≥5 invocations, ≥2 users) for the main leaderboard.

### 4.3 Report sections (org audience)
1. **Executive summary** — total skill invocations, active skill users, adoption % of Claude Code users, cost share attributed to skills
2. **Overall leaderboard** (composite score)
3. **Most adopted across teams** · **Fastest growing** · **Most reliable** · **Most expensive**
4. **Needs attention** — declining usage, low success rate, high rejection rate, top error types
5. **Team breakdown** — top skills per team; teams with low skill adoption
6. **By source** — built-in vs. org/custom vs. plugin/marketplace
7. **Data quality footer** — window, days from cache vs. freshly computed, % events missing `skill_name`

---

## 5. Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, `uv` + `pyproject.toml` |
| CLI | Typer + Rich |
| Cache | stdlib `sqlite3` (single local file) |
| Compute | Pure Python aggregation (no pandas needed for v1) |
| HTTP | httpx |
| Config | Pydantic Settings + YAML; secrets via env |
| Report | Jinja2 → single self-contained HTML file (inline CSS, lightweight inline-SVG charts) |
| Tests | pytest + synthetic telemetry fixtures |
| Local stack | docker-compose: OTel Collector + SigNoz |

---

## 6. Project Layout

```
skill-usage-ranker/
├── PLAN.md
├── README.md
├── pyproject.toml
├── ranker.yaml                     # weights, thresholds, source config
├── src/skill_ranker/
│   ├── cli.py                      # doctor | rank | report | export | cache
│   ├── config.py
│   ├── models.py                   # SkillEvent, DayPartial, SkillStats
│   ├── window.py                   # day-bucket planning, settle/TTL rules
│   ├── cache.py                    # SQLite schema, read/write, invalidation
│   ├── aggregate.py                # events → DayPartial; merge partials
│   ├── metrics.py                  # window metrics from partials
│   ├── ranking.py                  # scoring, smoothing, views
│   ├── adapters/
│   │   ├── base.py
│   │   ├── signoz.py
│   │   ├── otlp_jsonl.py           # tests / local dev
│   │   └── mappings/signoz.yaml
│   └── report/
│       ├── render.py
│       └── templates/report.html.j2
├── tests/
│   ├── fixtures/
│   └── test_*.py
├── scripts/gen_synthetic.py
└── deploy/
    ├── docker-compose.yml
    └── otel-collector.yaml         # incl. tool_parameters filter
```

---

## 7. CLI

```bash
skill-ranker doctor                              # SigNoz connectivity; warn if skill_name missing
skill-ranker rank   --window 30d --top 20        # uses cache, fills missing days only
skill-ranker rank   --window 30d --view growing --team payments
skill-ranker report --window 30d --out reports/skills-2026-11.html
skill-ranker export --window 30d --format csv --out skills.csv
skill-ranker cache  status                       # which days are cached / stale
skill-ranker cache  refresh --from 2026-11-01 --to 2026-11-07
skill-ranker cache  clear
```

Windows: `7d`, `30d`, `90d`, or `--from/--to` dates.

---

## 8. Milestones

| Phase | Scope | Exit criteria |
|---|---|---|
| **M0 — Discovery** (2–3 days) | docker-compose SigNoz; run Claude Code with telemetry; confirm Skill `tool_name`, where `skill_name` lands in SigNoz (attribute vs. JSON in `tool_parameters`), team attributes, and whether ClickHouse-SQL query API is usable | Golden fixtures + verified `mappings/signoz.yaml` |
| **M1 — Core + cache** (1 wk) | Models, window planner, SQLite cache, aggregator/merger, OTLP JSONL adapter, basic `rank` | Correct ranking on fixtures; 2nd run hits cache only |
| **M2 — SigNoz adapter** (1 wk) | `aggregate_day` (SQL) + `fetch_day` fallback, `doctor` | E2E against local SigNoz |
| **M3 — Ranking v1** (1 wk) | Composite score, smoothing, views, team breakdown | Matches hand-computed expectations |
| **M4 — HTML report** (3–4 days) | Self-contained report + CSV/JSON export | Shareable org report from real data |
| **M5 — More platforms** | Loki/Prometheus, Datadog, Honeycomb adapters | Each passes adapter contract tests |

---

## 9. Testing
- **Synthetic generator** — multiple teams/users, power-law skill popularity, failures, rejections, subagent inheritance, third-party plugins, late-arriving events.
- **Cache tests** — window overlap reuses days; today not permanently cached; `--refresh` and `schema_version` invalidate; merged partials == direct computation over the full window.
- **Adapter contract tests** — same seeded data → identical `DayPartial` from every adapter.
- **Ranking property tests** — monotonicity, smoothing, support thresholds.

---

## 10. Privacy & Security
- `user_key` = salted SHA-256 of `user.account_uuid` / `user.id`; salt from env; emails never stored.
- Report shows aggregates only; per-user data never rendered. Minimum group size (default 3 users) before a team row is shown.
- Collector `transform` processor keeps only `skill_name` from `tool_parameters` (drops Bash commands/tool input logged by `OTEL_LOG_TOOL_DETAILS`).
- No prompt/response logging needed.
- SigNoz API key via env var only; SQLite cache file created with `0600` permissions.

---

## 11. Decisions Log
- Teams: `OTEL_RESOURCE_ATTRIBUTES` (`team.id`, `department`) — verified present on every log record.
- SigNoz: **self-hosted**, run with Docker Compose from SigNoz **v0.129.0** (last release shipping compose files; newer releases use Foundry). Files in `deploy/signoz/docker`.
- **M4 complete (2026-11-18):** static HTML report (single file, no external requests, light/dark, phone layout, server-side SVG + table views, hover tooltips via textContent), `report` / `export --format csv|json` commands. Schema v2 adds per-team prompt/cost totals and active users (adoption % and skill cost share); verified SQL==Python on SigNoz. Remaining M3 items folded in: team breakdown with `min_team_users` privacy (small teams grouped; `--team` refused below threshold), minimum-sample guards for growth/decline/retention. Synthetic generator now includes non-skill prompts, skill-averse users and team affinities.
- **M2 complete (2026-11-18):** SigNoz adapter — 5 aggregate SQL queries in one `/api/v5/query_range` call (server-side name resolution mirroring the Python rules), raw-fetch fallback, raw user IDs hashed client-side (salt never sent), viewer-role API key, `doctor` checks (reachability, key, OTLP receiver/no-org, telemetry present, redaction %, team attribution). Live e2e contract test: SQL == fetch == JSONL for every day, with and without tool details. Found/handled: query-API float rounding, ClickHouse alias shadowing.
- **M1 complete (2026-11-18):** core models, window planner, SQLite day cache (compute-once, TTL for today, settle period, schema/salt guards), aggregation + merge, OTLP JSONL adapter, synthetic generator, composite scoring, `rank` / `doctor` / `cache` CLI; 38 tests. Incomplete days are excluded from trend/retention. Pulled forward from M3: composite score + smoothing + views (M3 now = tuning/validation, team breakdown with `min_team_users`).
- **M0 complete (2026-11-17):** events verified in SigNoz ClickHouse; ClickHouse SQL via `/api/v5/query_range` works → SigNoz adapter uses server-side `aggregate_day`. Details: `docs/telemetry-mapping.md`.

## 12. Open Questions
1. Should we detect **unused skills**? Needs an inventory source (org skills repo / plugin marketplace).
