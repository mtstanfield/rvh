"""Smoke tests for prompts/score_phase{1,2}.md templates."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prompts"))
import loader  # noqa: E402


def test_score_phase1_prompt_loads():
    rendered = loader.load_prompt(
        "score_phase1",
        layout_section="\n## This Repository's Layout\n\nProduction code: src/\n\n",
        path_list="src/auth/login.go\nsrc/util/clock.go",
    )
    assert "src/auth/login.go" in rendered
    assert "src/util/clock.go" in rendered
    assert "Production code: src/" in rendered
    assert "Priority scale" in rendered
    assert "JSON" in rendered  # output format must be specified


def test_score_phase1_prompt_no_layout():
    """layout_section is optional — empty string must not break rendering."""
    rendered = loader.load_prompt(
        "score_phase1",
        layout_section="",
        path_list="x.py",
    )
    assert "x.py" in rendered


def test_score_phase2_prompt_loads():
    rendered = loader.load_prompt(
        "score_phase2",
        layout_section="\n## This Repository's Layout\n\nProduction code: src/\n\n",
        files_section="### src/auth/login.go\n```\npackage auth\n```",
    )
    assert "src/auth/login.go" in rendered
    assert "package auth" in rendered
    assert "Production code: src/" in rendered
    assert "Priority scale" in rendered


def test_score_phase2_prompt_no_layout():
    rendered = loader.load_prompt(
        "score_phase2",
        layout_section="",
        files_section="### x.py\n```\nx = 1\n```",
    )
    assert "x.py" in rendered
    assert "x = 1" in rendered
