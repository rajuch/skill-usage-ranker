"""Minimal OTLP/HTTP JSON receiver for M0 discovery.

Captures whatever Claude Code exports (logs, metrics, traces) to JSONL files so we can
inspect the exact event/attribute shapes without needing a backend.

Usage:
    python3 scripts/otlp_capture.py --port 4318 --out tests/fixtures/raw

Point Claude Code at it with:
    OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SIGNALS = {"/v1/logs": "logs", "/v1/metrics": "metrics", "/v1/traces": "traces"}


def make_handler(out_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            signal = SIGNALS.get(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if signal is None:
                self.send_response(404)
                self.end_headers()
                return
            ctype = self.headers.get("Content-Type", "")
            if "json" not in ctype:
                # protobuf not decoded here; store raw so nothing is lost
                (out_dir / f"{signal}.bin").open("ab").write(body + b"\n---\n")
            else:
                payload = json.loads(body)
                with (out_dir / f"{signal}.jsonl").open("a") as f:
                    f.write(json.dumps(payload) + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, fmt, *args):
            print(f"[otlp-capture] {self.path} {args[1] if len(args) > 1 else ''}")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures/raw"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.out))
    print(f"[otlp-capture] listening on 127.0.0.1:{args.port}, writing to {args.out}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
