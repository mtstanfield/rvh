"""Seeding an eval run dir (scripts/eval_seed.py)."""
import pytest

import run_autonomous_v2
from common import connect_db, load_repo_profile, read_config
from eval_corpus import Anchor, Repo
from eval_seed import load_seed, seed_run

REPO = Repo(id="demo", source="x", commit="c" * 40, framework_hints=["django"])


def _checkout(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "gui").mkdir(parents=True)
    (checkout / "app.py").write_text("x\n")
    (checkout / "gui" / "views.py").write_text("y\n" * 50)
    return checkout


ANCHORS = [
    Anchor(id="a", repo="demo", path="app.py"),
    Anchor(id="b", repo="demo", path="gui/views.py", region=(10, 20), priority=4),
]


def test_one_pending_task_per_anchor_and_repeat_in_repeat_major_order(tmp_path):
    checkout = _checkout(tmp_path)
    run_dir = tmp_path / "run"
    seed = seed_run(run_dir, repo=REPO, checkout=checkout, anchors=ANCHORS, repeats=2)
    assert [(t["anchor"], t["repeat"]) for t in seed["tasks"].values()] == [("a", 0), ("b", 0), ("a", 1), ("b", 1)]
    assert load_seed(run_dir) == seed
    with connect_db(run_dir) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT files.path, tasks.priority, tasks.status, tasks.anchor_start_line, tasks.anchor_end_line"
            " FROM tasks JOIN files ON files.id = tasks.file_id ORDER BY tasks.id"
        )]
        assert rows[:2] == [("app.py", 5, "pending", None, None), ("gui/views.py", 4, "pending", 10, 20)]
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
        assert tuple(conn.execute("SELECT status, repo_root FROM runs").fetchone()) == ("scored", str(checkout))
        # The engine (run with --min-priority 1) sees every seeded task.
        assert len(run_autonomous_v2.pick_next_batch(conn, 1, 10)) == 4


def test_the_frozen_framework_hints_replace_analyze(tmp_path):
    run_dir = tmp_path / "run"
    seed_run(run_dir, repo=REPO, checkout=_checkout(tmp_path), anchors=ANCHORS, repeats=1)
    assert load_repo_profile(run_dir)["framework_hints"] == ["django"]
    config = read_config(run_dir)
    assert config["max_attempts"] == 3 and config["repo_root"].endswith("checkout")


def test_a_seeded_run_dir_is_never_reseeded(tmp_path):
    run_dir, checkout = tmp_path / "run", _checkout(tmp_path)
    seed_run(run_dir, repo=REPO, checkout=checkout, anchors=ANCHORS, repeats=1)
    with pytest.raises(FileExistsError, match="already seeded"):
        seed_run(run_dir, repo=REPO, checkout=checkout, anchors=ANCHORS, repeats=1)
