"""Generate realistic synthetic Claude Code skill telemetry as OTLP/JSON lines.

Shapes mirror the real events captured in M0 (see docs/telemetry-mapping.md):
user_prompt -> skill_activated -> tool_decision/tool_result (model-invoked) -> api_request(s).

    python3 scripts/gen_synthetic.py --days 30 --out tests/fixtures/synthetic
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# name, source, popularity weight, success prob, growth/day, mean cost per api call
SKILLS = [
    ("code-review", "bundled", 30, 0.98, 0.00, 0.020),
    ("simplify", "bundled", 14, 0.97, 0.00, 0.012),
    ("payments-runbook", "projectSettings", 12, 0.95, 0.03, 0.015),
    ("pr-description", "userSettings", 10, 0.99, 0.00, 0.004),
    ("db-migration-check", "policySettings", 8, 0.90, 0.02, 0.025),
    ("incident-triage", "policySettings", 6, 0.93, -0.03, 0.030),
    ("legacy-deploy", "projectSettings", 5, 0.70, -0.05, 0.018),
    ("api-docs-gen", "plugin", 4, 0.92, 0.06, 0.010),
    ("perf-profiler", "plugin", 2, 0.85, 0.00, 0.040),
    ("rarely-used", "userSettings", 0.3, 0.99, 0.00, 0.005),
]
TEAMS = ["payments", "platform", "growth", "mobile", "data"]
# team -> skills it leans toward (x3 weight)
TEAM_AFFINITY = {"payments": {"payments-runbook", "db-migration-check"},
                 "platform": {"incident-triage", "legacy-deploy", "perf-profiler"},
                 "growth": {"api-docs-gen", "pr-description"},
                 "mobile": {"perf-profiler", "simplify"},
                 "data": {"db-migration-check"}}
ERRORS = ["Error:ENOENT", "SkillLoadError", "Timeout"]


def attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": value}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def plain_prompt(rng: random.Random, person: dict, org: str, day: date) -> list[dict]:
    """A prompt that uses no skill: user_prompt + 1-4 api_requests without skill.name."""
    ts = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(seconds=rng.randint(0, 86399))
    pid, sid = str(uuid.UUID(int=rng.getrandbits(128))), str(uuid.UUID(int=rng.getrandbits(128)))
    recs = []
    for seq, (event, extra) in enumerate(
            [("user_prompt", {"prompt_length": str(rng.randint(5, 400)), "prompt": "<REDACTED>"})]
            + [("api_request", {"model": "claude-sonnet-5-5", "cost_usd": round(rng.expovariate(1 / 0.012), 6),
                                "input_tokens": rng.randint(2, 50), "output_tokens": rng.randint(20, 900)})
               for _ in range(rng.randint(1, 4))], start=1):
        t = ts + timedelta(milliseconds=1500 * seq)
        base = {"event.name": event, "event.timestamp": t.isoformat().replace("+00:00", "Z"),
                "event.sequence": seq, "session.id": sid, "prompt.id": pid,
                "user.account_uuid": person["uuid"], "organization.id": org,
                "team.id": person["team"], "department": "engineering", **extra}
        recs.append({"timeUnixNano": str(int(t.timestamp() * 1e9)),
                     "body": {"stringValue": f"claude_code.{event}"},
                     "attributes": [attr(k, v) for k, v in base.items()]})
    return recs


def generate(days: int, end: date, users: int, prompts_per_day: int, seed: int, redact: bool,
             out: Path, service_name: str = "claude-code") -> int:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    org = "org-synthetic-0001"
    # skill propensity per user: ~25% never use skills, the rest 10-70% of their prompts
    people = [{"uuid": str(uuid.UUID(int=rng.getrandbits(128))), "team": TEAMS[i % len(TEAMS)],
               "p_skill": 0.0 if rng.random() < 0.25 else rng.uniform(0.1, 0.7),
               "activity": rng.lognormvariate(0, 0.6)}
              for i in range(users)]
    activity = [p["activity"] for p in people]
    total = 0
    start = end - timedelta(days=days - 1)
    for d in range(days):
        day = start + timedelta(days=d)
        weights = [max(0.0, w * (1 + g) ** d) for _, _, w, _, g, _ in SKILLS]
        lines = []
        for _ in range(rng.randint(int(prompts_per_day * 0.7), int(prompts_per_day * 1.3))):
            person = rng.choices(people, weights=activity)[0]
            if rng.random() >= person["p_skill"]:
                lines.append((person["team"], plain_prompt(rng, person, org, day)))
                continue
            aff = TEAM_AFFINITY.get(person["team"], set())
            tw = [w * (3 if s[0] in aff else 1) for s, w in zip(SKILLS, weights)]
            name, source, _, p_ok, _, cost = rng.choices(SKILLS, weights=tw)[0]
            ts = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
                seconds=rng.randint(0, 86399))
            pid, sid = str(uuid.UUID(int=rng.getrandbits(128))), str(uuid.UUID(int=rng.getrandbits(128)))
            slash = rng.random() < 0.3
            hidden = redact and source in ("projectSettings", "userSettings", "policySettings")
            shown_name = "custom_skill" if hidden else ("third-party" if redact and source == "plugin" else name)
            seq = 0
            recs = []

            def ev(event, offset_ms, **extra):
                nonlocal seq
                seq += 1
                t = ts + timedelta(milliseconds=offset_ms)
                base = {"event.name": event, "event.timestamp": t.isoformat().replace("+00:00", "Z"),
                        "event.sequence": seq, "session.id": sid, "prompt.id": pid,
                        "user.account_uuid": person["uuid"], "organization.id": org,
                        "team.id": person["team"], "department": "engineering", **extra}
                recs.append({"timeUnixNano": str(int(t.timestamp() * 1e9)),
                             "body": {"stringValue": f"claude_code.{event}"},
                             "attributes": [attr(k, v) for k, v in base.items()]})

            ev("user_prompt", 0, prompt_length=str(rng.randint(5, 400)), prompt="<REDACTED>",
               **({"command_name": name, "command_source": "custom"} if slash else {}))
            tool_extra = {} if redact else {"tool_parameters": json.dumps({"skill_name": name})}
            if not slash:
                rejected = rng.random() < 0.03
                ev("tool_decision", 900, tool_name="Skill", decision="reject" if rejected else "accept",
                   source="user_reject" if rejected else "config", **tool_extra)
                if rejected:
                    lines.append((person["team"], recs))
                    continue
            ev("skill_activated", 950, **{"skill.name": shown_name,
                                          "invocation_trigger": "user-slash" if slash else "claude-proactive",
                                          "skill.source": source})
            if not slash:
                ok = rng.random() < p_ok
                ev("tool_result", 960, tool_name="Skill", success="true" if ok else "false",
                   duration_ms=str(int(rng.lognormvariate(3, 1))),
                   **({} if ok else {"error_type": rng.choice(ERRORS)}), **tool_extra)
            for k in range(rng.randint(1, 3)):
                ev("api_request", 2000 + 1500 * k, model="claude-sonnet-5-5",
                   cost_usd=round(rng.expovariate(1 / cost), 6), input_tokens=rng.randint(2, 50),
                   output_tokens=rng.randint(20, 900), cache_read_tokens=rng.randint(0, 30000),
                   **{"skill.name": "third-party" if redact and source == "plugin" else name})
            lines.append((person["team"], recs))

        path = out / f"{day.isoformat()}.jsonl"
        with path.open("w") as f:
            for team, recs in lines:
                payload = {"resourceLogs": [{
                    "resource": {"attributes": [attr("service.name", service_name),
                                                attr("team.id", team),
                                                attr("department", "engineering")]},
                    "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events"},
                                   "logRecords": recs}]}]}
                f.write(json.dumps(payload) + "\n")
        total += len(lines)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", type=date.fromisoformat, default=datetime.now(UTC).date())
    ap.add_argument("--users", type=int, default=40)
    ap.add_argument("--prompts-per-day", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--redact", action="store_true", help="simulate OTEL_LOG_TOOL_DETAILS off")
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures/synthetic"))
    ap.add_argument("--service-name", default="claude-code")
    a = ap.parse_args()
    n = generate(a.days, a.end, a.users, a.prompts_per_day, a.seed, a.redact, a.out, a.service_name)
    print(f"wrote {n} prompts over {a.days} days to {a.out}")


if __name__ == "__main__":
    main()
