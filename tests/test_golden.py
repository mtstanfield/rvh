"""Golden snapshots of everything rvh renders for a worker task.

The refactor on branch refactor/simplify is mostly deletion; these goldens
prove a deletion didn't change what the model or Docker receives. A task that
changes a golden on purpose regenerates it with

    RVH_UPDATE_GOLDENS=1 .venv/bin/python -m pytest -q tests/test_golden.py

and the `git diff tests/golden/` is the review. Only the render_* functions
change as the code's API changes; the golden files are the contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE = os.environ.get("RVH_UPDATE_GOLDENS") == "1"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REPO_ANALYSIS = {
    "production_paths": ["src/"],
    "test_paths": ["test/"],
    "ci_paths": [".github/"],
    "docs_paths": ["docs/"],
    "tooling_paths": ["tools/"],
    "generated_paths": [],
    "rationale": "C++ library with a small CLI.",
}
CPP_PAYLOAD = {
    "task_id": 7,
    "anchor_file": "src/scitokens_internal.cpp",
    "anchor_start_line": None,
    "anchor_end_line": None,
    "priority": 5,
    "budget_seconds": 3600,
    "report_path": "/output/task-7.md",
}
PY_REGION_PAYLOAD = {
    "task_id": 12,
    "anchor_file": "gui/views.py",
    "anchor_start_line": 1200,
    "anchor_end_line": 2400,
    "priority": 4,
    "budget_seconds": 1800,
    "report_path": "/output/task-12.md",
}
SALVAGE_TEXT = "PRIOR ANALYSIS: eval() on request.GET['q'] at views.py:88."
EVERYDAY_ARGV = [
    "--endpoint", "http://llm.example:8000", "--model", "qwen38-27b",
    "--api-key", "dummy", "--min-priority", "2", "--parallelism", "2",
]


def _normalize(text: str, tmp_path: Path) -> str:
    return (
        text.replace(sys.executable, "<PYTHON>")
        .replace(str(tmp_path.resolve()), "<TMP>")
        .replace(str(tmp_path), "<TMP>")
        .replace(str(REPO_ROOT), "<RVH>")
        .replace(str(Path.home()), "<HOME>")
        .replace(f"--user\n{os.getuid()}:{os.getgid()}", "--user\n<UID>:<GID>")
    )


def _check(name: str, text: str) -> None:
    path = GOLDEN_DIR / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"missing golden {path}; run with RVH_UPDATE_GOLDENS=1"
    assert text == path.read_text(encoding="utf-8"), (
        f"golden mismatch: {name} — regenerate with RVH_UPDATE_GOLDENS=1 and review git diff"
    )


# --- renderers: the only part that changes as the code's API changes -------

def render_worker_prompt_cpp() -> str:
    from worker import render_worker_prompt
    return render_worker_prompt(
        CPP_PAYLOAD,
        framework_hints=(), cpg_enabled=True,
    )


def render_worker_prompt_py_region() -> str:
    from worker import render_worker_prompt
    return render_worker_prompt(
        PY_REGION_PAYLOAD,
        framework_hints=("django",), cpg_enabled=False,
    )


def render_salvage_prompt() -> str:
    from worker import render_worker_prompt
    return render_worker_prompt(
        CPP_PAYLOAD,
        salvage_prior_analysis=SALVAGE_TEXT,
    )


def render_recipe() -> str:
    from worker import render_per_task_worker_recipe
    base = REPO_ROOT / "prompts" / "recipes" / "worker.yaml"
    return render_per_task_worker_recipe(render_worker_prompt_cpp(), base)


def render_system_prompt() -> str:
    from worker import GOOSE_SYSTEM_PROMPT
    return GOOSE_SYSTEM_PROMPT


def render_docker_argv(tmp_path: Path, monkeypatch) -> str:
    import worker
    fake = MagicMock()
    fake.DEVNULL = -3
    fake.PIPE = -1
    fake.Popen.return_value = MagicMock()
    fake.run.return_value = MagicMock(returncode=0)
    monkeypatch.setattr(worker, "subprocess", fake)
    repo = tmp_path / "repo"
    run_dir = repo / ".rvh-runs"
    worker.launch_worker_async(
        repo,
        render_worker_prompt_cpp(),
        run_dir / "tasks" / "auto-0001-deadbeef-task-7",
        image="rvh-worker:golden",
        network="rvh-repo-abc123-net",
        worker_endpoint="http://rvh-llm:8898",
        worker_api_key="sandboxed",
        worker_model="qwen38-27b",
        container_name="rvh-repo-abc123-w-src-scitokens_internal.cpp-7",
        cpg_endpoint="http://rvh-cpg:8080",
    )
    return "\n".join(fake.Popen.call_args.args[0]) + "\n"


def render_score_phase1() -> str:
    from prompts.loader import load_prompt
    from score_files import _format_repo_layout_block
    return load_prompt(
        "score_phase1",
        layout_section=f"\n{_format_repo_layout_block(REPO_ANALYSIS)}\n",
        path_list="src/scitokens.cpp\nsrc/scitokens_internal.cpp\ntest/main.cpp",
    )


FENCE = "`" * 3  # markdown code fence


def render_score_phase2() -> str:
    from prompts.loader import load_prompt
    from score_files import _format_repo_layout_block
    return load_prompt(
        "score_phase2",
        layout_section=f"\n{_format_repo_layout_block(REPO_ANALYSIS)}\n",
        files_section=(
            f"### src/scitokens_internal.cpp\n{FENCE}\n"
            f"int verify(const char *tok) {{ return 0; }}\n{FENCE}"
        ),
    )


def render_analyze_prompt() -> str:
    from analyze_repo import build_analysis_prompt
    return build_analysis_prompt(
        "src/\n  scitokens.cpp\n  scitokens_internal.cpp\ntest/\n  main.cpp\n",
        {"extensions": {".cpp": 3, ".h": 2}, "categories": {"code": 5}},
        ["cmake"],
        "SciTokens C++ library.",
    )


def render_engine_argv(tmp_path: Path, monkeypatch) -> str:
    import run_repo_vuln_hunt as rvh
    repo = tmp_path / "repo"
    args = rvh.build_parser().parse_args(["--repo", str(repo), *EVERYDAY_ARGV])
    cmd, env = rvh.build_engine_command(args, repo / ".rvh-runs", "rvh-worker:golden", "rvh-repo-abc123-net", True)
    return "\n".join(list(cmd) + [f"ENV {k}={v}" for k, v in sorted(env.items())]) + "\n"


# --- tests -----------------------------------------------------------------

def test_worker_prompt_cpp(tmp_path):
    _check("worker_prompt_cpp.txt", _normalize(render_worker_prompt_cpp(), tmp_path))


def test_worker_prompt_py_region(tmp_path):
    _check("worker_prompt_py_region.txt", _normalize(render_worker_prompt_py_region(), tmp_path))


def test_salvage_prompt(tmp_path):
    _check("salvage_prompt.txt", _normalize(render_salvage_prompt(), tmp_path))


def test_worker_recipe(tmp_path):
    _check("worker_recipe.yaml", _normalize(render_recipe(), tmp_path))


def test_system_prompt(tmp_path):
    _check("system_prompt.md", _normalize(render_system_prompt(), tmp_path))


def test_docker_argv(tmp_path, monkeypatch):
    _check("docker_argv.txt", _normalize(render_docker_argv(tmp_path, monkeypatch), tmp_path))


def test_score_phase1(tmp_path):
    _check("score_phase1.txt", _normalize(render_score_phase1(), tmp_path))


def test_score_phase2(tmp_path):
    _check("score_phase2.txt", _normalize(render_score_phase2(), tmp_path))


def test_analyze_prompt(tmp_path):
    _check("analyze_prompt.txt", _normalize(render_analyze_prompt(), tmp_path))


def test_engine_argv(tmp_path, monkeypatch):
    _check("engine_argv.txt", _normalize(render_engine_argv(tmp_path, monkeypatch), tmp_path))
