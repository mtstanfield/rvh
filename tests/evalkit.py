"""Builders for eval-harness tests: tiny git repos and corpora on disk."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

GIT = ["git", "-c", "user.email=eval@example.com", "-c", "user.name=eval", "-c", "commit.gpgsign=false"]

# A subset of evals/public/families.yaml — enough for the tests.
FAMILIES = {
    "code-injection": ["CWE-94", "CWE-95"],
    "command-injection": ["CWE-77", "CWE-78"],
    "xss": ["CWE-79"],
}

# Line 3 is the labeled sink (long enough to search for in tool output).
APP_PY = "import flask\n\nresult = eval(flask.request.args['q'])\nprint(result)\n"


def make_git_repo(root: Path, *commits: dict[str, str]) -> list[str]:
    """A git repo at ``root`` with one commit per {path: text} mapping; returns the commit shas."""
    subprocess.run([*GIT, "init", "-q", str(root)], check=True)
    shas = []
    for files in commits:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(root), "commit", "-qm", f"commit {len(shas) + 1}"], check=True)
        shas.append(subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
        ).stdout.strip())
    return shas


def label(**overrides) -> dict:
    """A valid label mapping (app.py:3, CWE-95); override any field."""
    base = {
        "id": "demo-eval-1", "repo": "demo", "file": "app.py", "sink_line": 3, "span": [2, 4],
        "cwe": "CWE-95", "family": "code-injection", "severity": "critical",
        "truth": "positive", "status": "confirmed", "evidence": "eval() of request data",
        "source": "harness", "source_ref": "notes.md", "reviewed_by": ["claude-code"],
        "labeled_by": {"job": "harvest", "prompt_version": 1, "date": "2026-09-24"},
    }
    return {**base, **overrides}


def anchor(**overrides) -> dict:
    """A valid anchor mapping (all of app.py); override any field."""
    return {**{"id": "demo-app", "repo": "demo", "path": "app.py", "tags": ["python"]}, **overrides}


def write_corpus(
    root: Path, *, repos: list[dict], labels: list[dict] = (),
    suites: dict[str, dict] | None = None, families: dict | None = None,
) -> Path:
    """Write a corpus directory; labels land in labels/<repo>.yaml by their ``repo``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "repos.yaml").write_text(yaml.safe_dump(repos, sort_keys=False))
    (root / "families.yaml").write_text(yaml.safe_dump(FAMILIES if families is None else families))
    (root / "labels").mkdir(exist_ok=True)
    by_repo: dict[str, list] = {}
    for item in labels:
        by_repo.setdefault(item["repo"], []).append(item)
    for repo_id, items in by_repo.items():
        (root / "labels" / f"{repo_id}.yaml").write_text(yaml.safe_dump(items, sort_keys=False))
    (root / "suites").mkdir(exist_ok=True)
    for suite_id, suite in (suites or {}).items():
        (root / "suites" / f"{suite_id}.yaml").write_text(yaml.safe_dump(suite, sort_keys=False))
    return root
