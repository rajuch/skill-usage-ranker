"""Render ReportData to a single self-contained HTML file (no external requests)."""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import asdict
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .build import DayPoint, ReportData

SOURCE_LABELS = {
    "bundled": "Built-in",
    "projectSettings": "Project",
    "userSettings": "Personal",
    "policySettings": "Org policy",
    "plugin": "Plugin",
    "unknown": "Unknown",
}


# ------------------------------------------------------------------ formatting


def compact(n: float | int | None) -> str:
    """1,284 · 12.9K · 4.2M — full figures under 10,000, compact above."""
    if n is None:
        return "–"
    if abs(n) < 10_000:
        return f"{n:,.0f}"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return f"{n:,.0f}"


def pct(x: float | None, digits: int = 0) -> str:
    return "–" if x is None else f"{x:.{digits}%}"


def signed_pct(x: float | None) -> str:
    return "–" if x is None else f"{x:+.0%}"


def money(x: float | None) -> str:
    if x is None:
        return "–"
    return f"${x:,.2f}" if x < 1000 else f"${compact(x)}"


def ms(x: float | None) -> str:
    if x is None:
        return "–"
    if math.isinf(x):
        return "> 60 s"
    return f"≤ {x / 1000:g} s" if x >= 1000 else f"≤ {x:.0f} ms"


# ------------------------------------------------------------------ chart geometry


def nice_max(v: float) -> tuple[float, list[float]]:
    """Clean axis maximum and 3-5 tick values."""
    if v <= 0:
        return 1.0, [0.0, 1.0]
    raw = v / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = step * math.ceil(v / step)
    return top, [step * i for i in range(int(round(top / step)) + 1)]


def column_chart(points: list[DayPoint], width: int = 760, height: int = 220) -> dict:
    left, right, top_pad, bottom = 44, 8, 12, 28
    pw, ph = width - left - right, height - top_pad - bottom
    vmax, ticks = nice_max(max((p.invocations for p in points), default=0))
    n = max(1, len(points))
    slot = pw / n
    bw = max(2.0, min(24.0, slot - 2))  # 2px surface gap between touching columns
    bars = []
    for i, p in enumerate(points):
        h = 0 if vmax == 0 else ph * p.invocations / vmax
        x = left + i * slot + (slot - bw) / 2
        y = top_pad + ph - h
        r = min(4.0, bw / 2, h)
        # rounded data-end (top), square at the baseline
        path = (f"M{x:.1f},{top_pad + ph:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                f"H{x + bw - r:.1f} Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{top_pad + ph:.1f} Z")
        bars.append({"path": path if h > 0 else "", "x": x, "w": bw, "hit_x": left + i * slot,
                     "hit_w": slot, "day": p.day.isoformat(), "label": f"{p.day:%b} {p.day.day}",
                     "value": p.invocations, "complete": p.complete})
    every = max(1, math.ceil(n / 8))
    xlabels = [{"x": b["x"] + b["w"] / 2, "text": b["label"]} for i, b in enumerate(bars)
               if i % every == 0 or i == n - 1]
    if len(xlabels) >= 2 and xlabels[-1]["x"] - xlabels[-2]["x"] < 50:
        xlabels.pop(-2)
    yticks = [{"y": top_pad + ph - ph * t / ticks[-1], "text": compact(t)} for t in ticks]
    return {"width": width, "height": height, "left": left, "right": width - right,
            "top": top_pad, "base": top_pad + ph, "bars": bars, "xlabels": xlabels, "yticks": yticks}


def sparkline(values: list[int], width: int = 96, height: int = 24) -> dict:
    if len(values) < 2:
        return {"points": "", "width": width, "height": height, "end": None}
    vmax = max(values) or 1
    pad = 3
    step = (width - 2 * pad) / (len(values) - 1)
    pts = [(pad + i * step, height - pad - (height - 2 * pad) * v / vmax) for i, v in enumerate(values)]
    return {"points": " ".join(f"{x:.1f},{y:.1f}" for x, y in pts), "width": width,
            "height": height, "end": pts[-1]}


# ------------------------------------------------------------------ rendering


def _env() -> Environment:
    env = Environment(loader=PackageLoader("skill_ranker", "report/templates"),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=True,
                      lstrip_blocks=True)
    env.filters.update(compact=compact, pct=pct, signed_pct=signed_pct, money=money, ms=ms,
                       sparkline=sparkline, source_label=lambda s: SOURCE_LABELS.get(s or "unknown", s))
    return env


def render_html(data: ReportData) -> str:
    return _env().get_template("report.html.j2").render(
        r=data, chart=column_chart(data.daily), spark_daily=sparkline(
            [p.invocations for p in data.daily if p.complete]))


def write_html(data: ReportData, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    return out


# ------------------------------------------------------------------ exports

EXPORT_FIELDS = [
    "rank", "skill_name", "skill_source", "score", "invocations", "slash_invocations",
    "unique_users", "unique_teams", "active_days", "success_rate", "acceptance_rate",
    "successes", "failures", "accepts", "rejects", "p50_duration_ms", "p95_duration_ms",
    "trend", "retention", "cost_usd", "cost_per_invocation", "input_tokens", "output_tokens",
]


def export_rows(data: ReportData) -> list[dict]:
    rows = []
    for lr in data.leaderboard:
        s = lr.stats
        d = asdict(s) | {"rank": lr.rank, "success_rate": s.success_rate,
                         "acceptance_rate": s.acceptance_rate,
                         "cost_per_invocation": s.cost_per_invocation}
        rows.append({k: d.get(k) for k in EXPORT_FIELDS})
    return rows


def to_csv(data: ReportData) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, lineterminator="\n")
    w.writeheader()
    for row in export_rows(data):
        # neutralize spreadsheet formula injection from telemetry-provided names
        name = str(row["skill_name"])
        if name[:1] in ("=", "+", "-", "@", "\t", "\r"):
            row["skill_name"] = "'" + name
        w.writerow({k: ("" if v is None or (isinstance(v, float) and math.isinf(v)) else v)
                    for k, v in row.items()})
    return buf.getvalue()


def to_json(data: ReportData) -> str:
    def clean(v):
        return None if isinstance(v, float) and (math.isinf(v) or math.isnan(v)) else v

    return json.dumps({
        "window": {"start": data.window.start.isoformat(), "end": data.window.end.isoformat()},
        "team": data.team, "generated_at": data.generated_at.isoformat(),
        "summary": asdict(data.summary),
        "skills": [{k: clean(v) for k, v in r.items()} for r in export_rows(data)],
        "teams": [asdict(t) for t in data.teams],
        "attention": [asdict(a) for a in data.attention],
        "below_threshold": data.quality.below_threshold,
    }, indent=2, default=str)
