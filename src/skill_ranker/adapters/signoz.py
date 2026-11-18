"""Self-hosted SigNoz adapter (verified against SigNoz v0.129, logs_v2 schema).

Fast path (`aggregate_day`): seven ClickHouse SQL queries sent in ONE `/api/v5/query_range`
request; SigNoz returns small per-skill aggregates instead of raw events. The name-resolution
rules mirror `normalize.records_to_events` so both paths produce identical DayPartials
(enforced by tests/test_signoz_e2e.py).

Raw user identifiers are returned to the client and hashed locally, so the hashing salt never
leaves this machine (the backend already holds the raw IDs).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..models import (
    DURATION_BUCKETS_MS,
    REDACTED_SKILL_NAMES,
    DayPartial,
    SkillDayRow,
    SkillEvent,
    TeamDayTotals,
)
from ..normalize import RawRecord, hash_user, records_to_events
from ..window import day_bounds
from .base import HealthReport

LOGS_TABLE = "signoz_logs.distributed_logs_v2"
FETCH_PAGE = 10_000
_SAFE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class SignozError(RuntimeError):
    pass


def _lit(s: str) -> str:
    if not _SAFE.match(s):
        raise ValueError(f"unsafe value for a SQL literal: {s!r}")
    return f"'{s}'"


def _events_cte(day: date, service_name: str) -> str:
    start, end = (int(t.timestamp()) for t in day_bounds(day))
    redacted = ", ".join(f"'{n}'" for n in sorted(REDACTED_SKILL_NAMES))
    bounds = "[" + ", ".join(str(b) for b in DURATION_BUCKETS_MS) + "]"
    return f"""
WITH
ev AS (
    SELECT
        replaceRegexpOne(attributes_string['event.name'], '^claude_code\\\\.', '') AS event,
        timestamp AS ts,
        toInt64(attributes_number['event.sequence']) AS seq,
        attributes_string['prompt.id'] AS pid,
        if(attributes_string['organization.id'] != '', attributes_string['organization.id'],
           resources_string['organization.id']) AS org,
        multiIf(resources_string['team.id'] != '', resources_string['team.id'],
                attributes_string['team.id'] != '', attributes_string['team.id'], 'unknown') AS team,
        if(attributes_string['user.account_uuid'] != '', attributes_string['user.account_uuid'],
           attributes_string['user.id']) AS user_raw,
        attributes_string['skill.name'] AS skill_attr,
        attributes_string['tool_name'] = 'Skill' AS is_skill_tool,
        multiIf(JSONExtractString(attributes_string['tool_parameters'], 'skill_name') != '',
                JSONExtractString(attributes_string['tool_parameters'], 'skill_name'),
                JSONExtractString(attributes_string['tool_input'], 'skill')) AS param,
        attributes_string['command_name'] AS cmd,
        attributes_string['invocation_trigger'] AS trig,
        attributes_string['skill.source'] AS src,
        lower(attributes_string['success']) AS success,
        attributes_string['decision'] AS decision,
        attributes_string['error_type'] AS error_type,
        multiIf(toFloat64OrNull(attributes_string['duration_ms']) IS NOT NULL,
                toFloat64OrNull(attributes_string['duration_ms']),
                mapContains(attributes_number, 'duration_ms'), attributes_number['duration_ms'],
                NULL) AS dur,
        attributes_number['cost_usd'] AS cost,
        toInt64(attributes_number['input_tokens']) AS in_tok,
        toInt64(attributes_number['output_tokens']) AS out_tok,
        toInt64(attributes_number['cache_read_tokens']) AS cr_tok
    FROM {LOGS_TABLE}
    WHERE ts_bucket_start BETWEEN {start - 1800} AND {end}
      AND timestamp >= {start}000000000 AND timestamp < {end}000000000
      AND resources_string['service.name'] = {_lit(service_name)}
      AND replaceRegexpOne(attributes_string['event.name'], '^claude_code\\\\.', '')
          IN ('skill_activated', 'tool_result', 'tool_decision', 'api_request', 'user_prompt')
),
hints AS (
    SELECT pid,
        argMinIf(param, (ts, seq), event IN ('tool_result', 'tool_decision') AND is_skill_tool AND param != '') AS h_tool,
        argMinIf(cmd, (ts, seq), event = 'user_prompt' AND cmd != '') AS h_cmd,
        argMinIf(skill_attr, (ts, seq), event = 'api_request' AND skill_attr NOT IN ({redacted})) AS h_api
    FROM ev WHERE pid != '' GROUP BY pid
),
act AS (
    SELECT ev.pid AS pid, argMin(
        multiIf(skill_attr NOT IN ({redacted}), skill_attr,
                h.h_tool != '', h.h_tool,
                trig = 'user-slash' AND h.h_cmd != '', h.h_cmd,
                h.h_api != '', h.h_api,
                skill_attr != '', skill_attr, 'unknown'), (ts, seq)) AS first_skill
    FROM ev LEFT JOIN hints h ON ev.pid = h.pid
    WHERE event = 'skill_activated' AND ev.pid != '' GROUP BY ev.pid
),
resolved AS (
    SELECT ev.*,
        multiIf(
            event = 'skill_activated',
                multiIf(skill_attr NOT IN ({redacted}), skill_attr,
                        h.h_tool != '', h.h_tool,
                        trig = 'user-slash' AND h.h_cmd != '', h.h_cmd,
                        h.h_api != '', h.h_api,
                        skill_attr != '', skill_attr, 'unknown'),
            event = 'tool_result',
                multiIf(param != '', param, a.first_skill != '', a.first_skill,
                        h.h_tool != '', h.h_tool, h.h_api != '', h.h_api, 'unknown'),
            event = 'tool_decision',
                multiIf(param != '', param, h.h_tool != '', h.h_tool, h.h_api != '', h.h_api, 'unknown'),
            multiIf(skill_attr NOT IN ({redacted}), skill_attr, a.first_skill != '', a.first_skill,
                    h.h_tool != '', h.h_tool, h.h_api != '', h.h_api, skill_attr)
        ) AS skill,
        arrayCount(b -> b < dur, {bounds}) AS bucket
    FROM ev
    LEFT JOIN hints h ON ev.pid = h.pid
    LEFT JOIN act a ON ev.pid = a.pid
    WHERE event = 'skill_activated'
       OR (event IN ('tool_result', 'tool_decision') AND is_skill_tool)
       OR (event = 'api_request' AND skill_attr != '')
)
"""


def _queries(day: date, service_name: str) -> dict[str, str]:
    cte = _events_cte(day, service_name)
    redacted = ", ".join(f"'{n}'" for n in sorted(REDACTED_SKILL_NAMES))
    return {
        "skills": cte + """
SELECT org, team, skill,
    argMinIf(src, (ts, seq), event = 'skill_activated' AND src != '') AS skill_source,
    countIf(event = 'skill_activated') AS invocations,
    countIf(event = 'skill_activated' AND trig = 'user-slash') AS slash_invocations,
    countIf(event = 'tool_result' AND success = 'true') AS successes,
    countIf(event = 'tool_result' AND success = 'false') AS failures,
    countIf(event = 'tool_decision' AND decision = 'accept') AS accepts,
    countIf(event = 'tool_decision' AND decision = 'reject') AS rejects,
    -- SigNoz's query API rounds float result columns (~3 significant digits), so cost is
    -- returned as integer nano-dollars and converted back client-side.
    toInt64(round(sumIf(cost, event = 'api_request') * 1e9)) AS cost_nano_usd,
    sumIf(in_tok, event = 'api_request') AS input_tokens,
    sumIf(out_tok, event = 'api_request') AS output_tokens,
    sumIf(cr_tok, event = 'api_request') AS cache_read_tokens
FROM resolved GROUP BY org, team, skill""",
        "hist": cte + """
SELECT org, team, skill, bucket, count() AS n
FROM resolved WHERE event = 'tool_result' AND dur IS NOT NULL
GROUP BY org, team, skill, bucket""",
        "users": cte + """
SELECT org, team, skill, user_raw, count() AS n
FROM resolved WHERE event = 'skill_activated' AND user_raw != ''
GROUP BY org, team, skill, user_raw""",
        "errors": cte + """
SELECT org, team, skill, error_type, count() AS n
FROM resolved WHERE event = 'tool_result' AND success = 'false' AND error_type != ''
GROUP BY org, team, skill, error_type""",
        "team_totals": cte + """
SELECT org, team, countIf(event = 'user_prompt') AS prompts,
    toInt64(round(sumIf(cost, event = 'api_request') * 1e9)) AS cost_nano_usd
FROM ev GROUP BY org, team""",
        "active_users": cte + """
SELECT org, team, user_raw, count() AS n
FROM ev WHERE event = 'user_prompt' AND user_raw != ''
GROUP BY org, team, user_raw""",
        # Separate query on purpose: ClickHouse lets SELECT aliases shadow columns in WHERE,
        # so a UNION branch selecting a literal `AS skill` would silently change the filter.
        "unresolved": cte + f"""
SELECT count() AS unresolved_n
FROM resolved WHERE event = 'skill_activated' AND skill IN ({redacted}, 'unknown')""",
    }


class SignozAdapter:
    def __init__(self, url: str, api_key: str, salt: str, *, service_name: str = "claude-code",
                 otlp_endpoint: str = "", timeout_s: float = 60.0):
        if not api_key:
            raise ValueError("SigNoz API key is empty; set the env var named by source.api_key_env")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.salt = salt
        self.service_name = service_name
        self.otlp_endpoint = otlp_endpoint.rstrip("/")
        self.timeout_s = timeout_s
        _lit(service_name)  # validate early
        self.source_id = f"signoz:{self.url}:{service_name}"

    # ------------------------------------------------------------------ HTTP

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "SigNoz-Api-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise SignozError(f"SigNoz {path} returned HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise SignozError(f"cannot reach SigNoz at {self.url}: {e}") from e
        if payload.get("status") != "success":
            err = payload.get("error", payload)
            raise SignozError(f"SigNoz query failed: {err.get('message', err) if isinstance(err, dict) else err}")
        return payload

    def query(self, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        """Run named ClickHouse SQL queries in one request; return rows per query name."""
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        body = {
            "schemaVersion": "v1", "start": now_ms - 86_400_000, "end": now_ms,
            "requestType": "raw",
            "compositeQuery": {"queries": [
                {"type": "clickhouse_sql", "spec": {"name": name, "query": sql}}
                for name, sql in queries.items()]},
        }
        results = self._post("/api/v5/query_range", body)["data"]["data"]["results"]
        out = {r["queryName"]: [row["data"] for row in r.get("rows") or []] for r in results}
        return {name: out.get(name, []) for name in queries}

    # ------------------------------------------------------------------ adapter API

    def aggregate_day(self, day: date) -> DayPartial:
        res = self.query(_queries(day, self.service_name))
        part = DayPartial(day=day)
        for r in res["skills"]:
            row = SkillDayRow(
                org_id=r["org"], team=r["team"], skill_name=r["skill"], skill_source=r["skill_source"],
                invocations=int(r["invocations"]), slash_invocations=int(r["slash_invocations"]),
                successes=int(r["successes"]), failures=int(r["failures"]),
                accepts=int(r["accepts"]), rejects=int(r["rejects"]), cost_usd=int(r["cost_nano_usd"]) / 1e9,
                input_tokens=int(r["input_tokens"]), output_tokens=int(r["output_tokens"]),
                cache_read_tokens=int(r["cache_read_tokens"]))
            part.skills[(row.org_id, row.team, row.skill_name)] = row
        for r in res["hist"]:
            key = (r["org"], r["team"], r["skill"])
            if key in part.skills:
                part.skills[key].duration_hist[int(r["bucket"])] += int(r["n"])
        for r in res["users"]:
            k = (r["org"], r["team"], r["skill"], hash_user(r["user_raw"], self.salt))
            part.users[k] = part.users.get(k, 0) + int(r["n"])
        for r in res["errors"]:
            part.errors[(r["org"], r["team"], r["skill"], r["error_type"])] = int(r["n"])
        part.unresolved_names = int(res["unresolved"][0]["unresolved_n"]) if res["unresolved"] else 0
        for r in res["team_totals"]:
            part.team_totals[(r["org"], r["team"])] = TeamDayTotals(int(r["prompts"]),
                                                                    int(r["cost_nano_usd"]) / 1e9)
        for r in res["active_users"]:
            k = (r["org"], r["team"], hash_user(r["user_raw"], self.salt))
            part.active_users[k] = part.active_users.get(k, 0) + int(r["n"])
        return part

    def fetch_day(self, day: date) -> list[SkillEvent]:
        """Fallback: page raw records for the day and normalize them client-side."""
        start, end = (int(t.timestamp()) for t in day_bounds(day))
        records: list[RawRecord] = []
        offset = 0
        while True:
            sql = f"""
SELECT timestamp, attributes_string AS s,
       -- numbers as strings: the query API rounds floats (~3 significant digits)
       mapApply((k, v) -> (k, toString(v)), attributes_number) AS n,
       attributes_bool AS b, resources_string AS r
FROM {LOGS_TABLE}
WHERE ts_bucket_start BETWEEN {start - 1800} AND {end}
  AND timestamp >= {start}000000000 AND timestamp < {end}000000000
  AND resources_string['service.name'] = {_lit(self.service_name)}
  AND replaceRegexpOne(attributes_string['event.name'], '^claude_code\\\\.', '')
      IN ('skill_activated', 'tool_result', 'tool_decision', 'api_request', 'user_prompt')
ORDER BY timestamp, id LIMIT {FETCH_PAGE} OFFSET {offset}"""
            rows = self.query({"A": sql})["A"]
            for row in rows:
                attrs = {**(row["s"] or {}), **(row["n"] or {}), **(row["b"] or {})}
                ts = datetime.fromtimestamp(int(row["timestamp"]) / 1e9, tz=UTC)
                records.append(RawRecord(ts=ts, attrs=attrs, resource=row["r"] or {}))
            if len(rows) < FETCH_PAGE:
                break
            offset += FETCH_PAGE
        return records_to_events(records, self.salt, self.service_name)

    # ------------------------------------------------------------------ doctor

    def healthcheck(self) -> HealthReport:
        checks: list[tuple[str, str, str]] = []

        def add(name: str, status: str, detail: str) -> None:
            checks.append((name, status, detail))

        try:
            with urllib.request.urlopen(self.url + "/api/v1/health", timeout=10) as r:
                add("SigNoz reachable", "ok" if r.status == 200 else "fail", f"{self.url} (HTTP {r.status})")
        except Exception as e:  # noqa: BLE001 - surface any connectivity failure
            add("SigNoz reachable", "fail", f"{self.url}: {e}")
            return HealthReport(checks)

        try:
            self.query({"A": "SELECT 1 AS one"})
            add("API key accepted", "ok", "ClickHouse SQL via /api/v5/query_range")
        except SignozError as e:
            add("API key accepted", "fail", str(e))
            return HealthReport(checks)

        if self.otlp_endpoint:
            add(*self._check_otlp())

        since = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
        svc = _lit(self.service_name)
        stats = self.query({"A": f"""
SELECT count() AS acts,
       countIf(attributes_string['skill.name'] IN ('custom_skill', 'third-party')) AS redacted,
       countIf(resources_string['team.id'] = '' AND attributes_string['team.id'] = '') AS no_team,
       uniqExact(attributes_string['skill.name']) AS skills,
       max(timestamp) AS last_ts
FROM {LOGS_TABLE}
WHERE ts_bucket_start >= {since - 1800} AND timestamp >= {since}000000000
  AND resources_string['service.name'] = {svc}
  AND replaceRegexpOne(attributes_string['event.name'], '^claude_code\\\\.', '') = 'skill_activated'"""})["A"][0]
        acts = int(stats["acts"])
        if acts == 0:
            add("skill telemetry (7d)", "fail",
                f"no skill_activated events for service.name={self.service_name} — is "
                "CLAUDE_CODE_ENABLE_TELEMETRY / OTEL_LOGS_EXPORTER=otlp rolled out?")
        else:
            last = datetime.fromtimestamp(int(stats["last_ts"]) / 1e9, tz=UTC)
            add("skill telemetry (7d)", "ok",
                f"{acts} activations, {stats['skills']} distinct names, latest {last:%Y-%m-%d %H:%M} UTC")
            red = int(stats["redacted"]) / acts
            add("skill names visible", "ok" if red < 0.05 else "warn",
                f"{red:.0%} redacted" + ("" if red < 0.05 else
                                         " — set OTEL_LOG_TOOL_DETAILS=1 for exact custom/plugin names"))
            nt = int(stats["no_team"]) / acts
            add("team attribution", "ok" if nt < 0.05 else "warn",
                f"{nt:.0%} without team.id" + ("" if nt < 0.05 else
                                              " — set team.id in OTEL_RESOURCE_ATTRIBUTES"))
        return HealthReport(checks)

    def _check_otlp(self) -> tuple[str, str, str]:
        """An empty OTLP export is harmless; a reset means receivers are down (e.g. no org yet)."""
        req = urllib.request.Request(self.otlp_endpoint + "/v1/logs", data=b'{"resourceLogs":[]}',
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return ("OTLP receiver accepting", "ok", f"{self.otlp_endpoint} (HTTP {r.status})")
        except urllib.error.HTTPError as e:
            return ("OTLP receiver accepting", "warn", f"{self.otlp_endpoint} returned HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            return ("OTLP receiver accepting", "fail",
                    f"{self.otlp_endpoint}: {e}. If SigNoz was just installed, create the first "
                    "admin/org — the collector only starts its OTLP receivers after that.")
