"""Unit tests for worker.extract_final_output.

The function parses goose's --output-format json stdout, finds the
recipe__final_output tool call, and returns its arguments dict (or
None if the session ended without calling final_output).
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from worker import extract_final_output  # noqa: E402


def _write_stdout(tmp_path: pathlib.Path, banner: str, body: dict) -> pathlib.Path:
    """Simulate goose stdout: a banner followed by --output-format json content."""
    p = tmp_path / "stdout.txt"
    p.write_text(banner + json.dumps(body, indent=2))
    return p


# Goose banner that prefixes every --output-format json session stdout.
_BANNER = (
    "\n    __( O)>  \xa0\xa0\xa0● new session · openai qwen36/worker\n"
    "   \\____)\xa0\xa0\xa020260526_1 · /workspace\n"
    "     L L\xa0\xa0\xa0goose is ready\n"
)


def test_extract_returns_args_for_successful_final_output(tmp_path):
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Begin."}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "abc",
                        "toolCall": {
                            "status": "success",
                            "value": {
                                "name": "recipe__final_output",
                                "arguments": {
                                    "verdict": "no-finding",
                                    "anchor_file": "/workspace/x.py",
                                    "task_id": 1,
                                    "summary": "ok",
                                    "findings": [],
                                },
                            },
                        },
                    }
                ],
            },
        ],
        "metadata": {"status": "completed"},
    }
    path = _write_stdout(tmp_path, _BANNER, body)
    out = extract_final_output(path)
    assert out is not None
    assert out["verdict"] == "no-finding"
    assert out["task_id"] == 1
    assert out["findings"] == []


def test_extract_returns_none_when_no_final_output_call(tmp_path):
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Begin."}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "abc",
                        "toolCall": {
                            "status": "success",
                            "value": {"name": "shell", "arguments": {"command": "ls"}},
                        },
                    }
                ],
            },
        ],
        "metadata": {"status": "completed"},
    }
    path = _write_stdout(tmp_path, _BANNER, body)
    assert extract_final_output(path) is None


def test_extract_returns_last_call_when_multiple_final_outputs(tmp_path):
    """If the model retries after a schema rejection, prefer the latest
    accepted call (matches goose's FinalOutputTool internal state)."""
    def asst(args):
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "toolRequest",
                    "id": "tc",
                    "toolCall": {
                        "status": "success",
                        "value": {"name": "recipe__final_output", "arguments": args},
                    },
                }
            ],
        }

    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Begin."}]},
            asst({"verdict": "no-finding", "anchor_file": "x", "task_id": 1, "summary": "first", "findings": []}),
            asst({"verdict": "no-finding", "anchor_file": "x", "task_id": 1, "summary": "second", "findings": []}),
        ],
        "metadata": {"status": "completed"},
    }
    path = _write_stdout(tmp_path, _BANNER, body)
    out = extract_final_output(path)
    assert out["summary"] == "second"


def test_extract_returns_none_for_truncated_stdout(tmp_path):
    """SIGKILL mid-write leaves a partial JSON. Don't raise — return None
    so the engine can route to salvage."""
    path = tmp_path / "stdout.txt"
    path.write_text(_BANNER + '{"messages": [{"role": "user", "content": ')  # cut off
    assert extract_final_output(path) is None


def test_extract_returns_none_for_missing_file(tmp_path):
    path = tmp_path / "nonexistent.txt"
    assert extract_final_output(path) is None


def test_extract_skips_failed_tool_calls(tmp_path):
    """A toolCall with status=error doesn't count as a successful final_output."""
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolRequest",
                        "id": "tc1",
                        "toolCall": {
                            "status": "error",
                            "value": {"name": "recipe__final_output", "arguments": {"bad": "shape"}},
                        },
                    }
                ],
            }
        ],
        "metadata": {"status": "completed"},
    }
    path = _write_stdout(tmp_path, _BANNER, body)
    assert extract_final_output(path) is None
