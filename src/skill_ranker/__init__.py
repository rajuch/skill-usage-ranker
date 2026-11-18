"""Skill Usage Ranker — rank Claude Code skills from OpenTelemetry telemetry."""

__version__ = "0.1.0"

# Bump whenever the shape or meaning of cached day partials changes; cached days with an
# older version are recomputed automatically.
SCHEMA_VERSION = 2  # v2: per-team prompt/cost totals + active users (adoption denominators)
