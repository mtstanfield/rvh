"""Pinned, history-free checkouts of corpus repos.

A corpus repo is pinned to a git commit, or — for a local directory that
isn't a git repo — to the git tree hash of its contents. ``ensure_checkout``
writes exactly that content to ``<cache>/src/<id>@<rev12>/`` with no ``.git``:
a worker that could run ``git log`` would see the fix commits. Reading goes
through a throwaway index, so a local source repo's index, HEAD, and working
tree are never touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import DEFAULT_RUNS_DIR
from eval_corpus import Repo


class CheckoutError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None, index: Path | None = None) -> str:
    env = dict(os.environ, GIT_INDEX_FILE=str(index)) if index else None
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, env=env)
    if r.returncode != 0:
        raise CheckoutError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def is_url(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def _git_dir(path: Path) -> Path | None:
    """The git dir of the repo rooted at ``path``; None for a plain directory."""
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    top, git_dir = r.stdout.splitlines()[:2]
    # A plain directory nested inside some other repo is still a plain directory.
    return Path(git_dir) if Path(top).resolve() == path.resolve() else None


def _has_commit(git_dir: Path, commit: str) -> bool:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True,
    ).returncode == 0


def snapshot_tree(path: Path, store: Path) -> str:
    """Store every file under ``path`` in the git object store ``store``; return the tree hash.

    Ignore rules don't apply (-f) and run state (.rvh-runs) is left out, so
    the hash depends only on the files' paths, modes, and contents.
    """
    if not (store / "HEAD").exists():
        _git("init", "-q", "--bare", str(store))
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        _git(f"--git-dir={store}", f"--work-tree={path}", "add", "-A", "-f", "--", ".",
             f":(exclude){DEFAULT_RUNS_DIR}", cwd=path, index=index)
        return _git(f"--git-dir={store}", "write-tree", index=index)


def _store_for(repo: Repo, cache: Path) -> Path:
    """The object store for a repo: one bare clone per upstream URL, shared by every
    pin of it (advisory mining adds a pre/fix pair per fix); a snapshot store per
    tree-pinned directory."""
    if repo.tree or not is_url(repo.source):
        return cache / "git" / f"{repo.id}.git"
    url = repo.source.split("://", 1)[-1].rstrip("/").removesuffix(".git")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", url).strip("-.")
    return cache / "git" / f"{slug}.git"


def _objects_for(repo: Repo, cache: Path) -> tuple[Path, str]:
    """(a git dir holding the pinned content, the commit or tree to read from it)."""
    store = _store_for(repo, cache)
    if repo.tree:
        actual = snapshot_tree(Path(repo.source).expanduser().resolve(), store)
        if actual != repo.tree:
            raise CheckoutError(
                f"{repo.id}: {repo.source} changed since it was pinned (tree {actual[:12]}, "
                f"pinned {repo.tree[:12]}); re-pin it with `./rvh eval pin`"
            )
        return store, repo.tree
    if is_url(repo.source):
        if not (store / "HEAD").exists():
            _git("clone", "-q", "--bare", repo.source, str(store))
        if not _has_commit(store, repo.commit):
            _git(f"--git-dir={store}", "fetch", "-q", "origin", repo.commit)
        git_dir = store
    else:
        git_dir = _git_dir(Path(repo.source).expanduser().resolve())
        if git_dir is None:
            raise CheckoutError(f"{repo.id}: {repo.source} is not a git repo; pin it by tree (`./rvh eval pin`)")
    if not _has_commit(git_dir, repo.commit):
        raise CheckoutError(f"{repo.id}: commit {repo.commit[:12]} is not in {repo.source}; fetch it first")
    return git_dir, repo.commit


def ensure_checkout(repo: Repo, cache: Path) -> Path:
    """The repo's pinned content under ``<cache>/src/``, written on first use and reused after."""
    dest = cache / "src" / f"{repo.id}@{repo.rev[:12]}"
    done = dest.with_name(dest.name + ".done")  # outside the tree the worker sees
    if done.exists():
        return dest
    if dest.exists():
        shutil.rmtree(dest)  # an earlier attempt died part-way
    (cache / "git").mkdir(parents=True, exist_ok=True)
    git_dir, treeish = _objects_for(repo, cache)
    dest.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        _git(f"--git-dir={git_dir}", "read-tree", treeish, index=index)
        _git(f"--git-dir={git_dir}", f"--work-tree={dest}", "checkout-index", "-a", index=index)
    # The worker mounts a tmpfs over <repo>/.rvh-runs; the mount point must exist.
    (dest / DEFAULT_RUNS_DIR).mkdir(exist_ok=True)
    done.touch()
    return dest


def pin(source: str) -> dict:
    """The repos.yaml pin for a URL or local path: {'commit': …} or {'tree': …}, plus any 'warning'."""
    if is_url(source):
        return {"commit": _git("ls-remote", source, "HEAD").split()[0]}
    path = Path(source).expanduser().resolve()
    if _git_dir(path) is not None:
        pinned = {"commit": _git("-C", str(path), "rev-parse", "HEAD")}
        if _git("-C", str(path), "status", "--porcelain", "--untracked-files=no"):
            pinned["warning"] = "working tree has uncommitted changes; the pin is HEAD, without them"
        return pinned
    with tempfile.TemporaryDirectory() as tmp:
        return {"tree": snapshot_tree(path, Path(tmp) / "store.git")}
