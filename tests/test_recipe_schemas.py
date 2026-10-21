"""The worker recipe carries the prompt and the final-output schema. The
provider, model and turn limit come from goose's CLI flags
(worker.build_goose_inner_args, tests/test_build_goose_inner_args.py), so the
recipe must not set them too. Schema content is checked in
tests/test_worker_recipe_schema.py."""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = REPO_ROOT / "prompts" / "recipes"


def test_worker_recipe_leaves_model_and_turns_to_the_cli():
    recipe = yaml.safe_load((RECIPES_DIR / "worker.yaml").read_text(encoding="utf-8"))
    assert "settings" not in recipe
    assert recipe["response"]["json_schema"]["type"] == "object"
