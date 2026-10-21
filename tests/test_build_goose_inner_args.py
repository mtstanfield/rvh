"""worker.build_goose_inner_args — goose's argv for one worker task (no docker prefix)."""
from __future__ import annotations

from worker import build_goose_inner_args


def _cmd(**overrides):
    kwargs = dict(model="qwen38-27b", recipe_path="/output/auto-0001-task-40-recipe.yaml", cpg_enabled=True)
    kwargs.update(overrides)
    return build_goose_inner_args(**kwargs)


def test_runs_the_per_task_recipe_non_interactively():
    cmd = _cmd()
    assert cmd[:2] == ["goose", "run"]
    assert cmd[cmd.index("--recipe") + 1] == "/output/auto-0001-task-40-recipe.yaml"
    assert "--params" not in cmd and "-i" not in cmd  # the prompt is baked into the recipe
    for flag in ("--no-session", "--no-profile", "--output-format"):
        assert flag in cmd


def test_turn_and_repetition_caps():
    cmd = _cmd()
    assert cmd[cmd.index("--max-turns") + 1] == "2000"
    assert cmd[cmd.index("--max-tool-repetitions") + 1] == "50"


def test_model_and_builtins():
    cmd = _cmd(model="glm-5.2")
    assert cmd[cmd.index("--model") + 1] == "glm-5.2"
    assert cmd[cmd.index("--with-builtin") + 1] == "developer,todo"


def test_cpg_extension_only_when_enabled():
    assert "python3 /opt/rvh/cpg_mcp_server.py" in _cmd(cpg_enabled=True)
    assert "--with-extension" not in _cmd(cpg_enabled=False)
