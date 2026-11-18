"""Normalization against real Claude Code telemetry captured in M0."""

from conftest import FIXTURES, SALT, load_records

from skill_ranker.normalize import hash_user, records_to_events


def _events(name: str):
    return records_to_events(load_records(FIXTURES / name), SALT)


def test_real_capture_with_tool_details():
    events = _events("logs_tooldetails_on.jsonl")
    acts = [e for e in events if e.kind == "activation"]
    assert [(e.skill_name, e.trigger) for e in acts] == [
        ("ranker-probe", "claude-proactive"),
        ("ranker-probe", "user-slash"),
    ]
    assert all(e.skill_source == "projectSettings" for e in acts)
    assert all(e.team == "payments" for e in acts)

    results = [e for e in events if e.kind == "tool_result"]
    assert len(results) == 1 and results[0].success is True and results[0].duration_ms == 7.0

    decisions = [e for e in events if e.kind == "decision"]
    assert [d.decision for d in decisions] == ["accept"]

    costs = [e for e in events if e.kind == "api_cost"]
    assert costs and all(e.skill_name == "ranker-probe" for e in costs)
    assert all(e.cost_usd > 0 for e in costs)


def test_redacted_name_is_recovered_from_api_request():
    """With OTEL_LOG_TOOL_DETAILS off, skill_activated says `custom_skill`."""
    raw = load_records(FIXTURES / "logs_tooldetails_off.jsonl")
    assert any(r.attrs.get("skill.name") == "custom_skill" for r in raw)
    acts = [e for e in records_to_events(raw, SALT) if e.kind == "activation"]
    assert [e.skill_name for e in acts] == ["ranker-probe"]


def test_user_ids_are_hashed_not_stored_raw():
    raw = load_records(FIXTURES / "logs_tooldetails_on.jsonl")
    uuids = {r.attrs.get("user.account_uuid") for r in raw} - {None}
    events = records_to_events(raw, SALT)
    keys = {e.user_key for e in events}
    assert not keys & uuids
    assert keys == {hash_user(u, SALT) for u in uuids}


def test_other_services_are_ignored():
    raw = load_records(FIXTURES / "logs_tooldetails_on.jsonl")
    for r in raw:
        r.resource["service.name"] = "some-other-app"
    assert records_to_events(raw, SALT) == []


def test_synthetic_redacted_names_resolved(synthetic_redacted_dir):
    events = []
    for f in sorted(synthetic_redacted_dir.glob("*.jsonl")):
        events += records_to_events(load_records(f), SALT)
    acts = [e for e in events if e.kind == "activation"]
    # custom skills: api_request.skill.name stays verbatim, so always recoverable
    assert "custom_skill" not in {e.skill_name for e in acts}
    assert "payments-runbook" in {e.skill_name for e in acts}
    # third-party plugins: only recoverable for /slash (command_name); model-invoked stay hidden
    assert all(e.skill_name != "third-party" for e in acts if e.trigger == "user-slash")
    plugin_slash = [e for e in acts if e.trigger == "user-slash" and e.skill_source == "plugin"]
    assert plugin_slash and all(e.skill_name in {"api-docs-gen", "perf-profiler"} for e in plugin_slash)
    assert any(e.skill_name == "third-party" for e in acts if e.trigger == "claude-proactive")
