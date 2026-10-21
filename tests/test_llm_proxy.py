"""scripts/llm_proxy.py — the buffering, key-holding gateway proxy."""
from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.error
import urllib.request

import pytest

import llm_proxy


def test_forward_headers_injects_the_real_key():
    headers = {"Authorization": "Bearer sandboxed", "Content-Type": "application/json",
               "Host": "rvh-llm:8898", "Content-Length": "2"}
    assert llm_proxy.forward_headers(headers, "real") == {
        "Content-Type": "application/json", "Authorization": "Bearer real",
    }


def test_forward_headers_keeps_the_callers_key_when_none_is_configured():
    assert llm_proxy.forward_headers({"Authorization": "Bearer dummy"}, "") == {"Authorization": "Bearer dummy"}


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        payload = (f'data: {{"auth": "{self.headers["Authorization"]}"}}\n\ndata: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _post(port: int) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=b"{}",
        headers={"Authorization": "Bearer sandboxed", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers["Content-Type"]
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers["Content-Type"]


def test_relays_the_whole_stream_with_the_real_key(monkeypatch):
    up = _serve(socketserver.TCPServer(("127.0.0.1", 0), _Upstream))
    monkeypatch.setattr(llm_proxy, "UPSTREAM", f"http://127.0.0.1:{up.server_address[1]}")
    monkeypatch.setattr(llm_proxy, "API_KEY", "real")
    px = _serve(llm_proxy.Server(("127.0.0.1", 0), llm_proxy.Handler))
    try:
        status, body, ctype = _post(px.server_address[1])
    finally:
        px.shutdown()
        up.shutdown()
    assert status == 200 and ctype == "text/event-stream"
    assert b"Bearer real" in body and b"[DONE]" in body


def test_unreachable_upstream_is_a_502(monkeypatch):
    monkeypatch.setattr(llm_proxy, "UPSTREAM", "http://127.0.0.1:9")  # discard port: nothing listens
    px = _serve(llm_proxy.Server(("127.0.0.1", 0), llm_proxy.Handler))
    try:
        status, body, _ = _post(px.server_address[1])
    finally:
        px.shutdown()
    assert status == 502 and b"upstream unreachable" in body
