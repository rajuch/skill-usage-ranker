# skill-usage-ranker

Rank Claude Code **skills** across an organization — usage, adoption across teams, reliability,
trend, retention and cost — from the OpenTelemetry telemetry Claude Code already emits.

- No raw-event database: each UTC day is computed **once** from the source and cached in a local
  SQLite file (`~/.skill-ranker/cache.db`). Re-runs and overlapping windows only compute missing days.
- Source adapters: OTLP JSONL files and self-hosted SigNoz; more platforms later.
- Design and roadmap: [PLAN.md](PLAN.md) · verified field mapping: [docs/telemetry-mapping.md](docs/telemetry-mapping.md)

## Why this exists

Organizations are adopting Claude Code skills quickly. Platform teams write them into org policy,
project repos ship their own, developers keep personal ones, and plugins add more from
marketplaces. Each skill is an investment: someone wrote it, reviewed it, and has to keep it
working as tools and codebases change.

Yet most organizations can't answer basic questions about that investment:

- **Which skills are actually used,** by how many people, and across how many teams?
- **Did a rollout work?** After shipping an org-wide skill, did teams adopt it, and did they keep using it?
- **Which skills are failing or getting rejected,** and with what errors?
- **Which skills are growing, and which are dying** and should be fixed or retired?
- **What do skills cost?** How much model spend happens while a skill is active?

### Why existing telemetry doesn't answer these questions

Claude Code already sends OpenTelemetry data, but it isn't skill analytics:

- **Skill usage is spread across several event types.** These are `skill_activated`,
  `tool_result`, `tool_decision`, `user_prompt` and `api_request`, and they have to be joined on
  `prompt.id` to make sense. A skill typed as `/slash` produces no tool events at all, so
  counting tool calls misses it.
- **Names are often hidden.** Unless `OTEL_LOG_TOOL_DETAILS=1` is set, custom skills appear as
  `custom_skill` and plugin skills as `third-party`. The real name has to be recovered from other
  events in the same prompt.
- **Observability dashboards show raw counts, not answers.** Adoption %, retention,
  week-over-week trend and a fair ranking need a definition and calculation that no dashboard
  provides out of the box. A fair ranking shouldn't let a skill with 2 successes out of 2 beat
  one with 950 out of 1,000.
- **The details are easy to get wrong.** Today's half-finished data makes every skill look like
  it's declining. Subagents inherit the parent's skill attribution. A backend query API can
  quietly round cost figures.
- **Rescanning months of logs for every report is slow and expensive.**

### Constraints

- **Privacy:** no prompts or responses are read, user IDs are stored only as salted hashes, and
  small teams can't be singled out.
- **Works with your existing telemetry backend:** SigNoz first, with others to follow, instead of
  requiring a new data pipeline.
- **Cheap to rerun:** each day is computed once and cached.
- **Shareable output:** a report a VP or a team lead can open, not a query only an SRE can write.

### What this project provides

A CLI that turns Claude Code's existing telemetry into a ranked, organization-wide view of skill
usage, and a single-file HTML report that platform teams can share to decide which skills to
invest in, fix or retire.

## Quick start (synthetic data)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/gen_synthetic.py --days 30          # -> tests/fixtures/synthetic/
export SKILL_RANKER_SALT='<a long random secret — keep it stable>'

.venv/bin/skill-ranker doctor
.venv/bin/skill-ranker rank --window 30d                     # computes 30 days, caches them
.venv/bin/skill-ranker rank --window 30d --view growing      # served from cache
.venv/bin/skill-ranker rank --window 30d --team payments --json
.venv/bin/skill-ranker cache status
```

Views: `overall` (composite score), `used`, `adopted`, `growing`, `reliable`, `expensive`.

## Report and exports

```bash
.venv/bin/skill-ranker report --window 30d                  # -> reports/skills-<start>_<end>.html
.venv/bin/skill-ranker report --window 30d --team payments -o payments.html
.venv/bin/skill-ranker export --window 30d --format csv -o skills.csv
.venv/bin/skill-ranker export --window 30d --format json
```

The report is one self-contained HTML file (inline CSS/SVG, no external requests) that works
offline, prints cleanly and follows the viewer's light/dark setting:
summary tiles (invocations, adoption, skills in use, cost share) · daily usage chart with table
view · leaderboard with sparklines · highlights · needs attention · teams · skill sources ·
data-quality footer. Teams with fewer than `ranking.min_team_users` users are grouped, and a
`--team` report is refused for such teams. Skill/team names are HTML-escaped and CSV cells are
protected against formula injection.

## Cache rules

| Day state | Behaviour |
|---|---|
| Complete (≥ `settle_hours` after UTC midnight) | computed once, cached permanently |
| Incomplete (today) | cached for `partial_day_ttl_minutes`, then recomputed; excluded from trend/retention |
| `--refresh` / `cache refresh --from --to` | recompute the range |
| Ranking weights changed | final ranking recomputed from cached days (no refetch) |
| `SCHEMA_VERSION` bumped | affected days recomputed automatically |

User identifiers are stored only as salted SHA-256 hashes. The cache refuses to mix salts.

## Self-hosted SigNoz

```bash
# 1. a SigNoz service account with the signoz-viewer role + API key (see docs/telemetry-mapping.md)
export SIGNOZ_API_KEY=... SKILL_RANKER_SALT=...
.venv/bin/skill-ranker doctor -c ranker.signoz.yaml
.venv/bin/skill-ranker rank -c ranker.signoz.yaml --window 30d
```

SigNoz aggregates each day server-side (small responses); only the per-day partials are cached
locally. Live contract test (ingests synthetic data under a unique `service.name`):

```bash
set -a; source deploy/signoz/.api-key.env; set +a
SIGNOZ_E2E=1 .venv/bin/pytest -q tests/test_signoz_e2e.py
```

## Sending real telemetry

See `deploy/claude-telemetry.env` (org rollout via managed settings) and `deploy/signoz/docker`
for the local self-hosted SigNoz stack (`docker compose -p signoz up -d`).

## Tests

```bash
.venv/bin/pytest -q
```
