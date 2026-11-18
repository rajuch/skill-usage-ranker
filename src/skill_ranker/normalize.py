"""Turn Claude Code OTLP log records into SkillEvents.

Field mapping is documented (and verified against real telemetry) in docs/telemetry-mapping.md.
Adapters that receive per-record attribute dicts call `records_to_events`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import REDACTED_SKILL_NAMES, SkillEvent

SKILL_TOOL = "Skill"
RELEVANT_EVENTS = {"skill_activated", "tool_result", "tool_decision", "api_request", "user_prompt"}


@dataclass(slots=True)
class RawRecord:
    """A log record flattened to plain dicts (backend-neutral)."""

    ts: datetime
    attrs: dict[str, Any]
    resource: dict[str, Any]


# ---------------------------------------------------------------- OTLP JSON parsing


def _otlp_value(v: dict[str, Any]) -> Any:
    if not v:
        return None
    kind, val = next(iter(v.items()))
    if kind == "intValue":
        return int(val)
    if kind == "doubleValue":
        return float(val)
    if kind == "arrayValue":
        return [_otlp_value(x) for x in val.get("values", [])]
    if kind == "kvlistValue":
        return {kv["key"]: _otlp_value(kv["value"]) for kv in val.get("values", [])}
    return val


def _otlp_attrs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: _otlp_value(a.get("value", {})) for a in items or []}


def iter_otlp_log_records(payload: dict[str, Any]) -> Iterator[RawRecord]:
    """Yield records from one OTLP/JSON ExportLogsServiceRequest."""
    for rl in payload.get("resourceLogs", []):
        resource = _otlp_attrs(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                attrs = _otlp_attrs(rec.get("attributes"))
                ns = rec.get("timeUnixNano") or rec.get("observedTimeUnixNano")
                if ns:
                    ts = datetime.fromtimestamp(int(ns) / 1e9, tz=UTC)
                elif attrs.get("event.timestamp"):
                    ts = datetime.fromisoformat(str(attrs["event.timestamp"]).replace("Z", "+00:00"))
                else:
                    continue
                yield RawRecord(ts=ts, attrs=attrs, resource=resource)


# ---------------------------------------------------------------- normalization


def hash_user(raw: str, salt: str) -> str:
    if not raw:
        return ""
    return hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()[:20]


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int:
    f = _float(v)
    return int(f) if f is not None else 0


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = _str(v).lower()
    return True if s == "true" else False if s == "false" else None


def _skill_param(attrs: dict[str, Any]) -> str:
    for key, field in (("tool_parameters", "skill_name"), ("tool_input", "skill")):
        raw = attrs.get(key)
        if not raw:
            continue
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict) and val.get(field):
            return str(val[field])
    return ""


def _event_name(attrs: dict[str, Any]) -> str:
    name = _str(attrs.get("event.name"))
    return name.removeprefix("claude_code.")


def records_to_events(records: Iterable[RawRecord], salt: str,
                      service_name: str = "claude-code") -> list[SkillEvent]:
    """Normalize raw records into SkillEvents, recovering redacted skill names.

    Resolution order for a redacted `skill_activated` name (custom_skill / third-party),
    within the same prompt.id:
      1. Skill tool_result / tool_decision `tool_parameters.skill_name`
      2. user_prompt `command_name` (slash invocations)
      3. the first api_request `skill.name` that is not redacted
    """
    recs = [
        r for r in records
        if _str(r.resource.get("service.name") or "claude-code") == service_name
        and _event_name(r.attrs) in RELEVANT_EVENTS
    ]
    recs.sort(key=lambda r: (r.ts, _int(r.attrs.get("event.sequence"))))

    # Per-prompt name hints.
    from_tool: dict[str, str] = {}
    from_cmd: dict[str, str] = {}
    from_api: dict[str, str] = {}
    for r in recs:
        a, name, pid = r.attrs, _event_name(r.attrs), _str(r.attrs.get("prompt.id"))
        if not pid:
            continue
        if name in ("tool_result", "tool_decision") and a.get("tool_name") == SKILL_TOOL:
            if (p := _skill_param(a)) and pid not in from_tool:
                from_tool[pid] = p
        elif name == "user_prompt" and a.get("command_name"):
            from_cmd.setdefault(pid, _str(a["command_name"]))
        elif name == "api_request":
            s = _str(a.get("skill.name"))
            if s not in REDACTED_SKILL_NAMES:
                from_api.setdefault(pid, s)

    def resolve(raw_name: str, pid: str, trigger: str = "") -> str:
        if raw_name not in REDACTED_SKILL_NAMES:
            return raw_name
        if pid in from_tool:
            return from_tool[pid]
        if trigger == "user-slash" and pid in from_cmd:
            return from_cmd[pid]
        if pid in from_api:
            return from_api[pid]
        return raw_name or "unknown"

    # Skill active in each prompt, used to attribute redacted api_request costs.
    active_skill: dict[str, str] = {}
    events: list[SkillEvent] = []
    for r in recs:
        a, name = r.attrs, _event_name(r.attrs)
        pid = _str(a.get("prompt.id"))
        team = _str(r.resource.get("team.id") or a.get("team.id")) or "unknown"
        base = dict(
            ts=r.ts,
            prompt_id=pid,
            session_id=_str(a.get("session.id")),
            user_key=hash_user(_str(a.get("user.account_uuid") or a.get("user.id")), salt),
            org_id=_str(a.get("organization.id") or r.resource.get("organization.id")),
            team=team,
        )
        if name == "user_prompt":
            events.append(SkillEvent(kind="prompt", skill_name="", **base))
        elif name == "api_request" and not a.get("skill.name"):
            events.append(SkillEvent(kind="api_other", skill_name="",
                                     cost_usd=_float(a.get("cost_usd")) or 0.0, **base))
        if name == "skill_activated":
            trigger = _str(a.get("invocation_trigger"))
            skill = resolve(_str(a.get("skill.name")), pid, trigger)
            active_skill[pid] = skill
            events.append(SkillEvent(kind="activation", skill_name=skill, trigger=trigger,
                                     skill_source=_str(a.get("skill.source")), **base))
        elif name == "tool_result" and a.get("tool_name") == SKILL_TOOL:
            skill = _skill_param(a) or active_skill.get(pid) or resolve("", pid)
            events.append(SkillEvent(kind="tool_result", skill_name=skill,
                                     success=_bool(a.get("success")),
                                     duration_ms=_float(a.get("duration_ms")),
                                     error_type=_str(a.get("error_type")), **base))
        elif name == "tool_decision" and a.get("tool_name") == SKILL_TOOL:
            skill = _skill_param(a) or resolve("", pid)
            events.append(SkillEvent(kind="decision", skill_name=skill,
                                     decision=_str(a.get("decision")), **base))
        elif name == "api_request" and a.get("skill.name"):
            raw = _str(a.get("skill.name"))
            skill = raw if raw not in REDACTED_SKILL_NAMES else active_skill.get(pid) or resolve(raw, pid)
            events.append(SkillEvent(kind="api_cost", skill_name=skill,
                                     cost_usd=_float(a.get("cost_usd")) or 0.0,
                                     input_tokens=_int(a.get("input_tokens")),
                                     output_tokens=_int(a.get("output_tokens")),
                                     cache_read_tokens=_int(a.get("cache_read_tokens")), **base))
    return events
