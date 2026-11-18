from __future__ import annotations

import os

from ..config import Config
from .base import HealthReport, SourceAdapter
from .otlp_jsonl import OtlpJsonlAdapter
from .signoz import SignozAdapter, SignozError

__all__ = ["HealthReport", "SourceAdapter", "OtlpJsonlAdapter", "SignozAdapter", "SignozError",
           "build_adapter"]


def build_adapter(cfg: Config) -> SourceAdapter:
    src = cfg.source
    if src.type == "otlp-jsonl":
        if not src.paths:
            raise ValueError("source.paths is required for the otlp-jsonl source")
        return OtlpJsonlAdapter(src.paths, cfg.salt, src.service_name)
    if src.type == "signoz":
        key = os.environ.get(src.api_key_env, "")
        if not key:
            raise ValueError(f"set {src.api_key_env} to a SigNoz API key (viewer role is enough)")
        return SignozAdapter(src.url, key, cfg.salt, service_name=src.service_name,
                             otlp_endpoint=src.otlp_endpoint, timeout_s=src.timeout_s)
    raise ValueError(f"unknown source type {src.type!r}")
