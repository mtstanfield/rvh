"""scripts/llm.py — the one host-side OpenAI-compatible chat client."""
from __future__ import annotations

import json
import urllib.error

import llm


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, payload: dict) -> dict:
    seen: dict = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return _Resp(payload)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_normalize_endpoint_strips_trailing_v1_and_slash():
    assert llm.normalize_endpoint("http://h:1246/v1/") == "http://h:1246"
    assert llm.normalize_endpoint("http://h:1246") == "http://h:1246"
    assert llm.normalize_endpoint(" https://x.example/direct/v1 ") == "https://x.example/direct"


def test_chat_posts_a_single_user_message_and_returns_stripped_content(monkeypatch):
    seen = _capture(monkeypatch, {"choices": [{"message": {"content": "  hello  "}}]})
    out = llm.chat("hi", endpoint="http://h:1/v1", model="m", api_key="k", timeout=9)
    assert out == "hello"
    assert seen["url"] == "http://h:1/v1/chat/completions"
    assert seen["body"] == {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    assert seen["auth"] == "Bearer k"
    assert seen["timeout"] == 9


def test_chat_sends_temperature_only_when_given(monkeypatch):
    seen = _capture(monkeypatch, {"choices": [{"message": {"content": "x"}}]})
    llm.chat("hi", endpoint="http://h", model="m", api_key="k", timeout=5, temperature=0.85)
    assert seen["body"]["temperature"] == 0.85


def test_chat_defaults_the_key_to_dummy(monkeypatch):
    seen = _capture(monkeypatch, {"choices": [{"message": {"content": "x"}}]})
    llm.chat("hi", endpoint="http://h", model="m", api_key=None, timeout=5)
    assert seen["auth"] == "Bearer dummy"


def test_chat_returns_empty_string_on_transport_error(monkeypatch, capsys):
    def boom(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    assert llm.chat("hi", endpoint="http://h", model="m", api_key="k", timeout=5, label="score") == ""
    assert "[score]" in capsys.readouterr().err


def test_chat_returns_empty_string_on_unexpected_payload(monkeypatch):
    _capture(monkeypatch, {"no_choices": True})
    assert llm.chat("hi", endpoint="http://h", model="m", api_key="k", timeout=5) == ""


def test_chat_treats_null_content_as_empty(monkeypatch):
    _capture(monkeypatch, {"choices": [{"message": {"content": None}}]})
    assert llm.chat("hi", endpoint="http://h", model="m", api_key="k", timeout=5) == ""
