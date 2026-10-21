#!/usr/bin/env python3
"""Buffering reverse proxy between goose and the model endpoint.

Runs inside the run's gateway container — the one thing sandboxed workers can
reach — and does two jobs:

1. Buffer each upstream response and hand it to goose whole. Reasoning-first
   models (GLM, gemma-4, qwen3.8) stream an empty ``content`` delta, then
   ``reasoning_content``, before any real content; goose's SSE parser ends the
   turn on that first empty delta and the worker silently does nothing.
   Buffered, goose sees every delta at once and aggregates them correctly.
2. Hold the real API key. Workers get a placeholder; when ``LLM_API_KEY`` is
   set the proxy sends it upstream in place of the caller's Authorization.

Requests go verbatim to ``LLM_UPSTREAM + path`` (goose sends ``/v1/...``).
Standard library only.

Env:
    LLM_UPSTREAM     upstream base URL without a trailing /v1 (required)
    LLM_PROXY_PORT   listen port (default 8898)
    LLM_API_KEY      key to send upstream (optional)
"""
from __future__ import annotations

import http.server
import os
import socketserver
import sys
import urllib.error
import urllib.request

UPSTREAM = os.environ.get("LLM_UPSTREAM", "").rstrip("/")
PORT = int(os.environ.get("LLM_PROXY_PORT", "8898"))
API_KEY = os.environ.get("LLM_API_KEY", "")
_HOP = {"host", "content-length", "transfer-encoding", "connection"}


def forward_headers(headers, api_key: str) -> dict[str, str]:
    """Headers to send upstream: hop-by-hop ones dropped, the real key injected."""
    out = {k: v for k, v in headers.items() if k.lower() not in _HOP}
    if api_key:
        out = {k: v for k, v in out.items() if k.lower() != "authorization"}
        out["Authorization"] = f"Bearer {api_key}"
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _proxy(self, method: str) -> None:
        body = None
        if "Content-Length" in self.headers:
            body = self.rfile.read(int(self.headers["Content-Length"]))
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, method=method,
            headers=forward_headers(self.headers, API_KEY),
        )
        try:
            # read() drains the whole (possibly SSE) response: that's the buffering.
            with urllib.request.urlopen(req, timeout=1800) as r:
                data, code = r.read(), r.getcode()
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, code, ctype = e.read(), e.code, "application/json"
        except urllib.error.URLError as e:
            data = f'{{"error":{{"message":"upstream unreachable: {e}"}}}}'.encode()
            code, ctype = 502, "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self._proxy("POST")

    def do_GET(self):
        self._proxy("GET")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if not UPSTREAM:
        sys.exit("LLM_UPSTREAM is required")
    with Server(("0.0.0.0", PORT), Handler) as server:
        print(f"llm_proxy (buffering) on :{PORT} -> {UPSTREAM}", file=sys.stderr, flush=True)
        server.serve_forever()
