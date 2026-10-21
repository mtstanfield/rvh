"""Tests for render_worker_prompt's per-anchor CPG gating.

The autonomous runner now decides per-anchor whether CPG tools are useful
(via cpg_build.anchor_supported_by_cpg) and passes the decision explicitly
to render_worker_prompt. The function still falls back to the legacy
RVH_CPG_ENDPOINT env probe when no override is provided, so callers that
haven't been updated keep their old behavior.
"""
from __future__ import annotations

from unittest.mock import patch

import worker


_PAYLOAD = {
    "anchor_file": "api/views.py",
    "report_path": "/output/task-1.md",
    "task_id": 1,
    "priority": 4,
}


def _patched_load_prompt(*, captured: dict):
    """Return a load_prompt stub that records its kwargs and emits a stub."""
    def fake(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        return f"<rendered:{name}>"
    return fake


def test_cpg_enabled_override_true_beats_unset_env(monkeypatch):
    monkeypatch.delenv("RVH_CPG_ENDPOINT", raising=False)
    captured: dict = {}
    with patch("prompts.loader.load_prompt", _patched_load_prompt(captured=captured)):
        worker.render_worker_prompt(
            _PAYLOAD,
            cpg_enabled=True,
        )
    assert captured["kwargs"]["cpg_enabled"] is True


def test_cpg_enabled_override_false_beats_set_env(monkeypatch):
    """Explicit False must NOT be overridden by an env var the parent set."""
    monkeypatch.setenv("RVH_CPG_ENDPOINT", "http://host.docker.internal:54321")
    captured: dict = {}
    with patch("prompts.loader.load_prompt", _patched_load_prompt(captured=captured)):
        worker.render_worker_prompt(
            _PAYLOAD,
            cpg_enabled=False,
        )
    assert captured["kwargs"]["cpg_enabled"] is False


def test_cpg_enabled_default_reads_env_when_unset(monkeypatch):
    monkeypatch.delenv("RVH_CPG_ENDPOINT", raising=False)
    captured: dict = {}
    with patch("prompts.loader.load_prompt", _patched_load_prompt(captured=captured)):
        worker.render_worker_prompt(
            _PAYLOAD,
        )
    assert captured["kwargs"]["cpg_enabled"] is False


def test_cpg_enabled_default_reads_env_when_set(monkeypatch):
    monkeypatch.setenv("RVH_CPG_ENDPOINT", "http://host.docker.internal:54321")
    captured: dict = {}
    with patch("prompts.loader.load_prompt", _patched_load_prompt(captured=captured)):
        worker.render_worker_prompt(
            _PAYLOAD,
        )
    assert captured["kwargs"]["cpg_enabled"] is True


