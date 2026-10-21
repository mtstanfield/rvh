"""Tests for stash_salvage_sidecar and clear_salvage_sidecar in run_autonomous_v2.

These helpers centralize the sidecar plumbing the autonomous runner uses
in two places: when a worker exits 0 but skipped its `write` call, and
(post-2026-05-06) when a worker is SIGKILLed for budget exhaustion. The
expected-report-missing path landed first; the budget-kill extension
reuses the same helpers so both flows produce the same .salvage/ artifact
layout for the next attempt's salvage_write recipe.
"""
from __future__ import annotations

import json
from pathlib import Path

from run_autonomous_v2 import (
    _salvage_sidecar_path,
    clear_salvage_sidecar,
    stash_salvage_sidecar,
)


_GOOSE_BANNER = (
    "\n"
    "    __( O)>  ● new session · openai qwen36/worker\n"
    "   \\____)    20260506_1 · /workspace\n"
    "     L L     goose is ready\n"
)


def _write_goose_stdout(stdout_path: Path, assistant_texts: list[str]) -> None:
    """Synthesize a goose --output-format json stdout: CLI banner + JSON."""
    payload = {
        "messages": [
            {"role": "assistant",
             "content": [{"type": "text", "text": t}]}
            for t in assistant_texts
        ]
    }
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(_GOOSE_BANNER + json.dumps(payload), encoding="utf-8")


def test_salvage_sidecar_path(tmp_path):
    p = _salvage_sidecar_path(tmp_path, 17)
    assert p == tmp_path / "reports" / ".salvage" / "task-17.txt"


def test_stash_salvage_sidecar_writes_extracted_text(tmp_path):
    stdout_path = tmp_path / "logs" / "stdout.txt"
    _write_goose_stdout(stdout_path, [
        "first turn analysis",
        "Conclusion: this file uses yaml.FullLoader on external HTTP response — finding.",
    ])
    n = stash_salvage_sidecar(tmp_path, 42, stdout_path)
    sidecar = tmp_path / "reports" / ".salvage" / "task-42.txt"
    assert sidecar.exists()
    body = sidecar.read_text(encoding="utf-8")
    assert "yaml.FullLoader" in body
    assert n == len(body)


def test_stash_salvage_sidecar_picks_last_assistant_turn(tmp_path):
    stdout_path = tmp_path / "stdout.txt"
    _write_goose_stdout(stdout_path, ["earlier turn", "final analysis line"])
    stash_salvage_sidecar(tmp_path, 5, stdout_path)
    body = (tmp_path / "reports" / ".salvage" / "task-5.txt").read_text()
    assert body == "final analysis line"


def test_stash_salvage_sidecar_returns_zero_when_no_text(tmp_path):
    stdout_path = tmp_path / "stdout.txt"
    _write_goose_stdout(stdout_path, [])  # messages=[], no assistant text
    assert stash_salvage_sidecar(tmp_path, 9, stdout_path) == 0
    assert not (tmp_path / "reports" / ".salvage" / "task-9.txt").exists()


def test_stash_salvage_sidecar_returns_zero_when_stdout_missing(tmp_path):
    """No goose output at all (e.g. SIGKILL before any tokens were flushed)."""
    assert stash_salvage_sidecar(tmp_path, 1, tmp_path / "missing.txt") == 0
    assert not (tmp_path / "reports" / ".salvage" / "task-1.txt").exists()


def test_stash_salvage_sidecar_returns_zero_when_only_whitespace(tmp_path):
    stdout_path = tmp_path / "stdout.txt"
    _write_goose_stdout(stdout_path, ["   \n\t  "])  # whitespace-only
    assert stash_salvage_sidecar(tmp_path, 2, stdout_path) == 0


def test_stash_salvage_sidecar_overwrites_existing(tmp_path):
    """Re-stashing on a retry must replace, not append."""
    stdout1 = tmp_path / "stdout1.txt"
    stdout2 = tmp_path / "stdout2.txt"
    _write_goose_stdout(stdout1, ["FIRST"])
    _write_goose_stdout(stdout2, ["SECOND"])
    stash_salvage_sidecar(tmp_path, 3, stdout1)
    stash_salvage_sidecar(tmp_path, 3, stdout2)
    body = (tmp_path / "reports" / ".salvage" / "task-3.txt").read_text()
    assert body == "SECOND"


def test_clear_salvage_sidecar_removes_existing(tmp_path):
    stdout_path = tmp_path / "stdout.txt"
    _write_goose_stdout(stdout_path, ["analysis"])
    stash_salvage_sidecar(tmp_path, 7, stdout_path)
    assert (tmp_path / "reports" / ".salvage" / "task-7.txt").exists()
    clear_salvage_sidecar(tmp_path, 7)
    assert not (tmp_path / "reports" / ".salvage" / "task-7.txt").exists()


def test_clear_salvage_sidecar_noop_when_missing(tmp_path):
    """No exception when there's nothing to clear."""
    clear_salvage_sidecar(tmp_path, 99)


def test_stash_creates_salvage_dir_on_first_use(tmp_path):
    """Mkdir parents=True covers a fresh run_dir without reports/.salvage/."""
    assert not (tmp_path / "reports" / ".salvage").exists()
    stdout_path = tmp_path / "stdout.txt"
    _write_goose_stdout(stdout_path, ["text"])
    stash_salvage_sidecar(tmp_path, 11, stdout_path)
    assert (tmp_path / "reports" / ".salvage").is_dir()
