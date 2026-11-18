"""Configuration: ranker.yaml + environment overrides."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

DEFAULT_CACHE_PATH = Path.home() / ".skill-ranker" / "cache.db"


@dataclass(slots=True)
class Weights:
    invocations: float = 0.25
    unique_users: float = 0.20
    unique_teams: float = 0.15
    success_rate: float = 0.15
    acceptance_rate: float = 0.05
    retention: float = 0.10
    trend: float = 0.05
    cost_per_invocation: float = 0.05  # subtracted


@dataclass(slots=True)
class RankingConfig:
    weights: Weights = field(default_factory=Weights)
    min_invocations: int = 5
    min_users: int = 2
    smoothing_strength: float = 10.0  # pseudo-observations pulling rates toward the org mean
    min_team_users: int = 3  # privacy: hide team rows with fewer users


@dataclass(slots=True)
class CacheConfig:
    path: Path = DEFAULT_CACHE_PATH
    settle_hours: float = 2.0
    partial_day_ttl_minutes: float = 15.0

    @property
    def settle(self) -> timedelta:
        return timedelta(hours=self.settle_hours)

    @property
    def partial_ttl(self) -> timedelta:
        return timedelta(minutes=self.partial_day_ttl_minutes)


@dataclass(slots=True)
class SourceConfig:
    type: str = "otlp-jsonl"
    # otlp-jsonl
    paths: list[str] = field(default_factory=list)
    # signoz
    url: str = "http://localhost:8080"
    api_key_env: str = "SIGNOZ_API_KEY"
    service_name: str = "claude-code"
    otlp_endpoint: str = ""  # e.g. http://localhost:4318 — enables the doctor ingest check
    timeout_s: float = 60.0


@dataclass(slots=True)
class Config:
    source: SourceConfig = field(default_factory=SourceConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    salt_env: str = "SKILL_RANKER_SALT"

    @property
    def salt(self) -> str:
        salt = os.environ.get(self.salt_env, "")
        if not salt:
            raise RuntimeError(
                f"Set {self.salt_env} to a secret, stable value; it is used to hash user IDs "
                "so the cache never stores raw identities."
            )
        return salt

    def ranking_fingerprint(self) -> str:
        """Hash of everything that changes final rankings but not day partials."""
        blob = json.dumps(asdict(self.ranking), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is None:
        candidate = Path("ranker.yaml")
        path = candidate if candidate.exists() else None
    if path is not None:
        data = yaml.safe_load(path.read_text()) or {}
        src = data.get("source", {})
        cfg.source = SourceConfig(**src) if src else cfg.source
        c = data.get("cache", {})
        if c:
            cfg.cache = CacheConfig(
                path=Path(os.path.expanduser(c.get("path", str(DEFAULT_CACHE_PATH)))),
                settle_hours=c.get("settle_hours", 2.0),
                partial_day_ttl_minutes=c.get("partial_day_ttl_minutes", 15.0),
            )
        r = data.get("ranking", {})
        if r:
            weights = Weights(**r.pop("weights", {}))
            cfg.ranking = RankingConfig(weights=weights, **r)
        cfg.salt_env = data.get("salt_env", cfg.salt_env)
    if env_url := os.environ.get("SIGNOZ_URL"):
        cfg.source.url = env_url
    if env_cache := os.environ.get("SKILL_RANKER_CACHE"):
        cfg.cache.path = Path(env_cache)
    return cfg
