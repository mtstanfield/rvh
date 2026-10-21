"""Pinned checkouts for evals (scripts/eval_checkout.py)."""
import os
import subprocess

import pytest

from eval_checkout import CheckoutError, ensure_checkout, pin
from eval_corpus import Repo
from tests.evalkit import make_git_repo


def test_checkout_writes_the_pinned_commit_without_history(tmp_path):
    src = tmp_path / "src"
    old, _new = make_git_repo(src, {"app.py": "vulnerable\n"}, {"app.py": "fixed\n"})
    dest = ensure_checkout(Repo(id="demo", source=str(src), commit=old), tmp_path / "cache")
    assert (dest / "app.py").read_text() == "vulnerable\n"
    assert not (dest / ".git").exists()
    assert (dest / ".rvh-runs").is_dir()  # the worker's tmpfs mount point


def test_checkout_leaves_the_source_repo_alone(tmp_path):
    src = tmp_path / "src"
    (sha,) = make_git_repo(src, {"app.py": "committed\n"})
    (src / "app.py").write_text("local edit\n")
    dest = ensure_checkout(Repo(id="demo", source=str(src), commit=sha), tmp_path / "cache")
    assert (dest / "app.py").read_text() == "committed\n"
    assert (src / "app.py").read_text() == "local edit\n"
    status = subprocess.run(["git", "-C", str(src), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout.strip() == "M app.py"


def test_a_finished_checkout_is_reused(tmp_path):
    src = tmp_path / "src"
    (sha,) = make_git_repo(src, {"app.py": "x\n"})
    repo = Repo(id="demo", source=str(src), commit=sha)
    dest = ensure_checkout(repo, tmp_path / "cache")
    (dest / "marker").write_text("kept")
    assert ensure_checkout(repo, tmp_path / "cache") == dest
    assert (dest / "marker").exists()


def test_url_sources_are_cloned_into_the_cache(tmp_path):
    src = tmp_path / "src"
    (sha,) = make_git_repo(src, {"app.py": "x\n"})
    dest = ensure_checkout(Repo(id="demo", source=src.as_uri(), commit=sha), tmp_path / "cache")
    assert (dest / "app.py").read_text() == "x\n"
    assert len(list((tmp_path / "cache" / "git").glob("*.git"))) == 1


def test_a_missing_commit_is_an_error(tmp_path):
    src = tmp_path / "src"
    make_git_repo(src, {"app.py": "x\n"})
    with pytest.raises(CheckoutError, match="is not in"):
        ensure_checkout(Repo(id="demo", source=str(src), commit="b" * 40), tmp_path / "cache")


def test_plain_directories_are_pinned_by_tree_hash(tmp_path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "app.py").write_text("x\n")
    (src / ".gitignore").write_text("*.py\n")  # ignore rules must not drop files
    (src / ".rvh-runs").mkdir()
    (src / ".rvh-runs" / "state.db").write_text("run state")  # never part of the snapshot
    pinned = pin(str(src))
    assert set(pinned) == {"tree"}
    dest = ensure_checkout(Repo(id="demo", source=str(src), tree=pinned["tree"]), tmp_path / "cache")
    assert (dest / "app.py").read_text() == "x\n"
    assert not (dest / ".rvh-runs" / "state.db").exists()


def test_the_tree_hash_ignores_timestamps_but_not_content(tmp_path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "app.py").write_text("x\n")
    first = pin(str(src))["tree"]
    os.utime(src / "app.py", (0, 0))
    assert pin(str(src))["tree"] == first
    (src / "app.py").write_text("y\n")
    assert pin(str(src))["tree"] != first


def test_a_changed_plain_directory_is_refused(tmp_path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "app.py").write_text("x\n")
    tree = pin(str(src))["tree"]
    (src / "app.py").write_text("changed\n")
    with pytest.raises(CheckoutError, match="changed since it was pinned"):
        ensure_checkout(Repo(id="demo", source=str(src), tree=tree), tmp_path / "cache")


def test_pin_of_a_git_repo_is_head_and_warns_when_dirty(tmp_path):
    src = tmp_path / "src"
    (sha,) = make_git_repo(src, {"app.py": "x\n"})
    assert pin(str(src)) == {"commit": sha}
    assert pin(src.as_uri()) == {"commit": sha}
    (src / "app.py").write_text("edited\n")
    assert pin(str(src))["warning"].startswith("working tree has uncommitted changes")


def test_pins_of_one_url_share_a_single_clone(tmp_path):
    """Advisory mining adds a pre/fix entry per advisory; each must not re-clone the upstream."""
    src = tmp_path / "src"
    old, new = make_git_repo(src, {"app.py": "vulnerable\n"}, {"app.py": "fixed\n"})
    cache = tmp_path / "cache"
    pre = ensure_checkout(Repo(id="demo-pre-x", source=src.as_uri(), commit=old), cache)
    fix = ensure_checkout(Repo(id="demo-fix-x", source=src.as_uri(), commit=new), cache)
    assert ((pre / "app.py").read_text(), (fix / "app.py").read_text()) == ("vulnerable\n", "fixed\n")
    assert len(list((cache / "git").iterdir())) == 1
