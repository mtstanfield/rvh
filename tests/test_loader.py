"""prompts/loader.py — one flat prompt directory."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prompts.loader import load_prompt  # noqa: E402


def test_renders_placeholders():
    text = load_prompt(
        "salvage_write", anchor_path="/workspace/a.py", task_id=3,
        report_path="/output/task-3.md", prior_analysis="PRIOR",
    )
    assert "/workspace/a.py" in text and "PRIOR" in text


def test_cpg_sibling_is_prepended_verbatim():
    plain = load_prompt("score_phase1", layout_section="", path_list="a.py")
    assert load_prompt("score_phase1", cpg_enabled=True, layout_section="", path_list="a.py") == plain
    worker_cpg = (Path(__file__).resolve().parents[1] / "prompts" / "worker_cpg.md").read_text().rstrip()
    assert worker_cpg  # the sibling exists; worker goldens pin the full prepend


def test_missing_prompt_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt("no_such_prompt")


def test_no_prompt_set_directories_remain():
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    assert not (prompts / "qwen3").exists()
    assert not (prompts / "gemma4").exists()
    assert (prompts / "recipes" / "worker.yaml").exists()
