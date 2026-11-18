# Telemetry Mapping (M0 — verified)

Captured from **Claude Code 2.1.326** on 2026-11-17 using `scripts/otlp_capture.py` (OTLP/HTTP JSON).
Golden fixtures (PII-scrubbed): `tests/fixtures/{logs,metrics}_tooldetails_{on,off}.jsonl`.

## 1. Primary signal: `skill_activated` (log event)

Not in the public docs page we initially read, but emitted for **every** skill activation —
both model-invoked and `/slash`-invoked.

| Attribute | Example | Notes |
|---|---|---|
| `event.name` | `skill_activated` | log body = `claude_code.skill_activated` |
| `skill.name` | `ranker-probe` | ⚠ redacted to `custom_skill` for user/project skills when `OTEL_LOG_TOOL_DETAILS` is off |
| `invocation_trigger` | `claude-proactive`, `user-slash` | model-chosen vs. user-typed |
| `skill.source` | `projectSettings` | expect also `userSettings`, `policySettings`, `plugin`, `bundled`… (to confirm) |
| `prompt.id`, `session.id`, `event.timestamp`, `event.sequence` | | join keys |

**Decision:** `skill_activated` is the **invocation counter** (replaces counting `tool_result` +
`user_prompt.command_name` separately, which would double count / miss slash invocations).

## 2. Supporting signals

| Event | Fires when | Useful fields | Observed |
|---|---|---|---|
| `tool_decision` | Skill tool call is permission-checked | `tool_name=Skill`, `decision`, `source`, `tool_parameters={"skill_name":…}` (details on) | model-invoked only |
| `tool_result` | Skill tool call completes | `tool_name=Skill`, `success`, `duration_ms`, `tool_use_id`, `tool_parameters`, `tool_input={"skill":…}` (details on) | model-invoked only; **no** tool events for `/slash` |
| `user_prompt` | user submits a prompt | `command_name=ranker-probe`, `command_source=custom` | slash only |
| `api_request` | each model call | `skill.name` (verbatim even with details off), `cost_usd`, tokens, `model` | present on requests made while skill active |

- `success`, `duration_ms`, `prompt_length` arrive as **strings**; tokens/cost as numbers → normalizer must coerce.
- Resource attributes (`department`, `team.id`) are **copied onto every log record's attributes** as well as the resource.
- Disallowing the Skill tool (`--disallowedTools Skill`) produces **no** Skill events at all (model fell back to reading SKILL.md via Bash) → acceptance/rejection is only meaningful for interactive permission prompts.

## 3. Metrics

| Metric | Temporality | Skill attribution |
|---|---|---|
| `claude_code.cost.usage` | DELTA (`aggregationTemporality=1`) | separate series with `skill.name` |
| `claude_code.token.usage` | DELTA | separate series with `skill.name` + `type` |
| `claude_code.session.count`, `active_time.total` | DELTA | none |

Metrics carry `team.id` / `department` as datapoint attributes. Cost can come from either
`api_request` events or metrics — **use events** (exact, per-request, joinable by `prompt.id`).

## 4. Name resolution rules (normalizer)

1. `skill_activated.skill.name` if not `custom_skill` / `third-party`.
2. Else `tool_parameters.skill_name` / `tool_input.skill` from the matching `tool_result` (same `prompt.id`, nearest `event.sequence`).
3. Else `user_prompt.command_name` (same `prompt.id`) for `user-slash` triggers.
4. Else first `api_request.skill.name` after the activation in the same `prompt.id`.
5. Else keep the redacted value and count it in the report's data-quality footer.

## 5. Team / org dimensions

| Dimension | Attribute | Source |
|---|---|---|
| Team | `team.id` | `OTEL_RESOURCE_ATTRIBUTES` (managed settings) |
| Department | `department` | `OTEL_RESOURCE_ATTRIBUTES` |
| Org | `organization.id` | standard attr |
| User key | salted hash of `user.account_uuid` → fallback `user.id` | standard attrs |

## 6. SigNoz storage (M0 part 2 — verified on self-hosted SigNoz v0.129.0)

Local stack: `deploy/signoz/docker` (`docker compose -p signoz up -d`). UI: http://localhost:8080.

- Table: `signoz_logs.distributed_logs_v2`. `body` = `claude_code.<event>`.
- `attributes_string`: `event.name`, `skill.name`, `invocation_trigger`, `skill.source`, `tool_name`,
  `success` (string `"true"/"false"`), tool `duration_ms` (string), `tool_parameters` (JSON string),
  `command_name`, `prompt.id`, `session.id`, `team.id`, `department`.
- `attributes_number`: `cost_usd`, `input_tokens`, `output_tokens`, api `duration_ms`, `event.sequence`.
- `resources_string`: `service.name='claude-code'`, `team.id`, `department`.
- Time: `timestamp` (ns, UInt64); `ts_bucket_start` (s, 30-min buckets) → use for partition pruning.
- Query API: `POST /api/v5/query_range` with `{"type":"clickhouse_sql"}` works → **server-side `aggregate_day`
  is viable**. Draft query: `src/skill_ranker/adapters/sql/signoz_skill_day.sql` (validated).
- Auth: dev used session login (`/api/v2/sessions/email_password`); adapter will use an API key
  (`SIGNOZ-API-KEY` header) created in Settings → API Keys.

### Setup gotcha
The collector's OTLP receivers (4317/4318) **do not start until an org exists** — SigNoz logs
`cannot create agent without orgId` and connections are reset. Create the first admin
(UI signup or `POST /api/v1/register`) before sending telemetry. `skill-ranker doctor` should detect this.

### Query API gotchas (found in M2)
- **Float precision:** `/api/v5/query_range` rounds float result values — both top-level columns
  and values inside `attributes_number` maps — to ~3 significant digits (0.04971 → 0.0498).
  The adapter returns cost as integer nano-dollars and fetches numeric maps via
  `mapApply((k, v) -> (k, toString(v)), attributes_number)`.
- **Alias shadowing:** ClickHouse lets SELECT aliases shadow columns in WHERE
  (`SELECT '' AS skill ... WHERE skill IN (...)` filters on the literal). Avoid reusing column
  names as aliases of literals, especially in UNION branches.
- **No row cap** observed (25k rows returned in one response, empty `nextCursor`); `fetch_day` still pages by 10k.
- **API keys** belong to service accounts: `POST /api/v1/service_accounts` → assign the
  `signoz-viewer` role (`POST …/{id}/roles`) → `POST …/{id}/keys`. Header: `SigNoz-Api-Key`.

### Still open
- [ ] `skill.source` values for user-level, plugin and bundled skills.
- [ ] Interactive rejection → `tool_decision.decision=reject` with `source=user_reject`.
