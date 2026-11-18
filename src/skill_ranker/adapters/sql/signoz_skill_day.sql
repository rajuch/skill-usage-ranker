-- Per-day, per-team, per-skill partial aggregates (SigNoz logs_v2 schema, v0.129).
-- Params: {day_start:UInt64} / {day_end:UInt64} as unix seconds (UTC day bounds).
WITH events AS (
    SELECT
        attributes_string['event.name']                             AS event,
        attributes_string['prompt.id']                              AS prompt_id,
        coalesce(nullIf(resources_string['team.id'], ''), nullIf(attributes_string['team.id'], ''), 'unknown') AS team,
        attributes_string['skill.name']                             AS skill_attr,
        JSONExtractString(attributes_string['tool_parameters'], 'skill_name') AS skill_param,
        attributes_string['invocation_trigger']                     AS trigger,
        attributes_string['skill.source']                           AS skill_source,
        attributes_string['success']                                AS success,
        toUInt64OrNull(attributes_string['duration_ms'])            AS tool_duration_ms,
        attributes_number['cost_usd']                               AS cost_usd,
        attributes_number['input_tokens']                           AS in_tok,
        attributes_number['output_tokens']                          AS out_tok,
        coalesce(nullIf(attributes_string['user.account_uuid'], ''), attributes_string['user.id']) AS user_raw
    FROM signoz_logs.distributed_logs_v2
    WHERE ts_bucket_start BETWEEN {day_start:UInt64} - 1800 AND {day_end:UInt64}
      AND timestamp >= {day_start:UInt64} * 1000000000 AND timestamp < {day_end:UInt64} * 1000000000
      AND resources_string['service.name'] = 'claude-code'
      AND attributes_string['event.name'] IN ('skill_activated', 'tool_result', 'api_request')
),
-- resolve redacted names (custom_skill/third-party) via other events in the same prompt
names AS (
    SELECT prompt_id,
           anyIf(skill_param, event = 'tool_result' AND skill_param != '')                          AS from_tool,
           anyIf(skill_attr, event = 'api_request' AND skill_attr NOT IN ('', 'custom_skill', 'third-party')) AS from_api
    FROM events GROUP BY prompt_id
)
SELECT
    e.team AS team,
    multiIf(e.skill_attr NOT IN ('custom_skill', 'third-party'), e.skill_attr,
            n.from_tool != '', n.from_tool, n.from_api != '', n.from_api, e.skill_attr) AS skill_name,
    any(e.skill_source)                                  AS skill_source,
    count()                                              AS invocations,
    countIf(e.trigger = 'user-slash')                    AS slash_invocations,
    uniqExact(e.user_raw)                                AS unique_users
FROM events e LEFT JOIN names n USING prompt_id
WHERE e.event = 'skill_activated'
GROUP BY team, skill_name
ORDER BY invocations DESC
