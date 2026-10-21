"""The labeled eval corpus: repos, CWE families, labels, and suites.

A corpus is a directory — the private ``rvh-evals`` repo, or ``evals/public/``
in this repo:

    repos.yaml           [{id, source, commit | tree, framework_hints, cpg_lang}]
    families.yaml        {family: [CWE-…, …]}
    labels/<repo>.yaml   [label, …] — ground truth for one repo
    suites/<id>.yaml     {kind, repeats, frozen, anchors: [anchor, …]}

``load_corpus`` checks every field and raises ``CorpusError`` listing every
problem at once. The checks that need more than the YAML — labels against
the pinned code, the public-export guard, human sign-off — are separate
functions that return a list of problems.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

SEVERITIES = ("info", "low", "medium", "high", "critical")
TRUTHS = ("positive", "negative")
STATUSES = ("confirmed", "gated", "disputed")
SOURCES = ("disclosure", "harness", "frontier-review", "claude-review", "advisory", "human")
# A positive from one of these sources needs a human's sign-off before a suite
# holding it is frozen.
MODEL_ONLY_SOURCES = ("frontier-review", "claude-review")
SUITE_KINDS = ("quick", "full", "demo")
CWE_RE = re.compile(r"^CWE-\d+$")
PIN_RE = re.compile(r"^[0-9a-f]{40}$")


class CorpusError(ValueError):
    """The corpus is malformed; the message lists every problem found."""


@dataclass
class Repo:
    id: str
    source: str  # a git URL, or a local path (a git repo or a plain directory)
    commit: str | None = None
    tree: str | None = None  # for a plain directory: the git tree hash of its contents
    framework_hints: list[str] = field(default_factory=list)
    cpg_lang: str | None = None  # CPG language; default: detected from the seeded anchors

    @property
    def rev(self) -> str:
        return self.commit or self.tree or ""


@dataclass
class Label:
    id: str
    repo: str
    file: str
    sink_line: int
    span: tuple[int, int]
    cwe: str
    family: str
    severity: str
    truth: str
    status: str
    evidence: str
    source: str
    source_ref: str
    reviewed_by: list[str] = field(default_factory=list)
    labeled_by: dict = field(default_factory=dict)
    # Other fair sink locations for the same bug (a mass-assignment flaw is
    # equally "at" the permitted-params block and the create call).
    also: list[tuple[str, tuple[int, int]]] = field(default_factory=list)

    @property
    def locations(self) -> list[tuple[str, tuple[int, int]]]:
        return [(self.file, self.span), *self.also]


@dataclass
class Anchor:
    id: str
    repo: str
    path: str
    region: tuple[int, int] | None = None
    exhaustive: bool = False
    tags: list[str] = field(default_factory=list)
    priority: int = 5  # the task priority the worker sees; 5 also gets the longest budget


@dataclass
class Suite:
    id: str
    kind: str
    repeats: int
    frozen: bool
    anchors: list[Anchor]
    sha256: str  # of the suite file, so a comparison can tell suites apart

    def by_repo(self) -> dict[str, list[Anchor]]:
        """Anchors grouped by repo, in suite order."""
        grouped: dict[str, list[Anchor]] = {}
        for anchor in self.anchors:
            grouped.setdefault(anchor.repo, []).append(anchor)
        return grouped


@dataclass
class Corpus:
    root: Path
    repos: dict[str, Repo]
    families: dict[str, str]  # CWE → family
    labels: dict[str, Label]
    suites: dict[str, Suite]

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    def family_of(self, cwe: str | None) -> str | None:
        """The CWE's family; a CWE families.yaml doesn't list is its own family."""
        if not cwe:
            return None
        return self.families.get(cwe, cwe)

    def repo_labels(self, repo_id: str) -> list[Label]:
        return [label for label in self.labels.values() if label.repo == repo_id]

    def labels_in_scope(self, anchor: Anchor) -> list[Label]:
        """Labels on the anchor's file whose sink line is inside its region, if it has one."""
        return [
            label for label in self.repo_labels(anchor.repo)
            if label.file == anchor.path
            and (anchor.region is None or anchor.region[0] <= label.sink_line <= anchor.region[1])
        ]


class _Fields:
    """Typed reads from one YAML mapping, recording problems instead of raising."""

    def __init__(self, raw, where: str, errors: list[str]):
        self.raw = raw if isinstance(raw, dict) else {}
        self.where = where
        self.errors = errors
        if not isinstance(raw, dict):
            errors.append(f"{where}: expected a mapping")

    def _bad(self, key: str, want: str) -> None:
        self.errors.append(f"{self.where}: {key!r} must be {want}")

    def text(self, key: str, *, choices: tuple[str, ...] = (), required: bool = True) -> str | None:
        value = self.raw.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip():
            self._bad(key, "a non-empty string")
            return None
        if choices and value not in choices:
            self._bad(key, f"one of {', '.join(choices)}")
            return None
        return value.strip()

    def integer(self, key: str, *, default: int | None = None, hi: int | None = None) -> int | None:
        value = self.raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (hi and value > hi):
            self._bad(key, "a positive integer" + (f" no greater than {hi}" if hi else ""))
            return None
        return value

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw.get(key, default)
        if not isinstance(value, bool):
            self._bad(key, "true or false")
            return default
        return value

    def texts(self, key: str) -> list[str]:
        value = self.raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            self._bad(key, "a list of strings")
            return []
        return [v.strip() for v in value]

    def line_range(self, key: str, *, required: bool) -> tuple[int, int] | None:
        value = self.raw.get(key)
        if value is None and not required:
            return None
        if (not isinstance(value, list) or len(value) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in value)
                or value[0] > value[1]):
            self._bad(key, "[first, last] line numbers")
            return None
        return (value[0], value[1])


def _parse_repo(raw, where: str, errors: list[str]) -> Repo | None:
    f = _Fields(raw, where, errors)
    before = len(errors)
    repo = Repo(
        id=f.text("id"), source=f.text("source"),
        commit=f.text("commit", required=False), tree=f.text("tree", required=False),
        framework_hints=f.texts("framework_hints"), cpg_lang=f.text("cpg_lang", required=False),
    )
    pins = [pin for pin in (repo.commit, repo.tree) if pin]
    if len(pins) != 1:
        errors.append(f"{where}: needs exactly one of 'commit' or 'tree'")
    elif not PIN_RE.match(pins[0]):
        errors.append(f"{where}: a pin is a full 40-character git hash")
    return repo if len(errors) == before else None


def _parse_label(raw, where: str, families: dict[str, str], errors: list[str]) -> Label | None:
    f = _Fields(raw, where, errors)
    before = len(errors)
    by = _Fields(f.raw.get("labeled_by"), f"{where}.labeled_by", errors)
    label = Label(
        id=f.text("id"), repo=f.text("repo"), file=f.text("file"),
        sink_line=f.integer("sink_line"), span=f.line_range("span", required=True),
        cwe=f.text("cwe"), family=f.text("family"), severity=f.text("severity", choices=SEVERITIES),
        truth=f.text("truth", choices=TRUTHS), status=f.text("status", choices=STATUSES),
        evidence=f.text("evidence"), source=f.text("source", choices=SOURCES),
        source_ref=f.text("source_ref"), reviewed_by=f.texts("reviewed_by"),
        labeled_by={"job": by.text("job"), "prompt_version": by.integer("prompt_version"),
                    "date": str(by.raw.get("date") or "")},
    )
    raw_also = f.raw.get("also") or []
    if not isinstance(raw_also, list):
        raw_also = [None]
    for item in raw_also:
        span = item.get("span") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or not isinstance(item.get("file"), str) or not isinstance(span, list)
                or len(span) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in span)
                or span[0] > span[1]):
            errors.append(f"{where}: 'also' entries need a file and a [first, last] span")
        else:
            label.also.append((item["file"], (span[0], span[1])))
    if not label.labeled_by["date"]:
        errors.append(f"{where}.labeled_by: 'date' is required")
    if label.cwe and not CWE_RE.match(label.cwe):
        errors.append(f"{where}: 'cwe' must look like CWE-79")
    elif label.cwe and label.family and label.family != families.get(label.cwe, label.cwe):
        errors.append(
            f"{where}: family must be {families.get(label.cwe, label.cwe)!r} for {label.cwe} "
            "(to group this CWE with others, add it to families.yaml)"
        )
    if label.span and label.sink_line and not label.span[0] <= label.sink_line <= label.span[1]:
        errors.append(f"{where}: sink_line {label.sink_line} is outside span {list(label.span)}")
    return label if len(errors) == before else None


def _parse_anchor(raw, where: str, errors: list[str]) -> Anchor | None:
    f = _Fields(raw, where, errors)
    before = len(errors)
    anchor = Anchor(
        id=f.text("id"), repo=f.text("repo"), path=f.text("path"),
        region=f.line_range("region", required=False), exhaustive=f.flag("exhaustive", False),
        tags=f.texts("tags"), priority=f.integer("priority", default=5, hi=5),
    )
    return anchor if len(errors) == before else None


def _read_yaml(path: Path, errors: list[str], default):
    if not path.exists():
        return default
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        errors.append(f"{path.name}: {exc}")
        return default
    return default if data is None else data


def _parse_suite(path: Path, repos: dict[str, Repo], errors: list[str]) -> Suite | None:
    where = f"suites/{path.name}"
    f = _Fields(_read_yaml(path, errors, {}), where, errors)
    before = len(errors)
    kind = f.text("kind", choices=SUITE_KINDS)
    repeats = f.integer("repeats", default=1)
    frozen = f.flag("frozen", False)
    raw_anchors = f.raw.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        errors.append(f"{where}: 'anchors' must be a non-empty list")
        raw_anchors = []
    anchors: list[Anchor] = []
    for i, raw in enumerate(raw_anchors):
        anchor = _parse_anchor(raw, f"{where}.anchors[{i}]", errors)
        if anchor is None:
            continue
        if anchor.repo not in repos:
            errors.append(f"{where}.anchors[{i}]: unknown repo {anchor.repo!r}")
        if any(a.id == anchor.id for a in anchors):
            errors.append(f"{where}.anchors[{i}]: duplicate anchor id {anchor.id!r}")
        anchors.append(anchor)
    if len(errors) != before:
        return None
    return Suite(id=path.stem, kind=kind, repeats=repeats, frozen=frozen, anchors=anchors,
                 sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _load_families(path: Path, errors: list[str]) -> dict[str, str]:
    raw = _read_yaml(path, errors, {})
    if not isinstance(raw, dict):
        errors.append("families.yaml: expected a mapping of family → [CWE, ...]")
        return {}
    cwe_to_family: dict[str, str] = {}
    for family, cwes in raw.items():
        if not isinstance(cwes, list):
            errors.append(f"families.yaml: {family!r} must list its CWEs")
            continue
        for cwe in cwes:
            if not isinstance(cwe, str) or not CWE_RE.match(cwe):
                errors.append(f"families.yaml: {family!r} lists a malformed CWE {cwe!r}")
            elif cwe in cwe_to_family:
                errors.append(f"families.yaml: {cwe} is in both {cwe_to_family[cwe]!r} and {family!r}")
            else:
                cwe_to_family[cwe] = str(family)
    return cwe_to_family


def load_corpus(root: Path) -> Corpus:
    """Read and check a corpus directory; raise CorpusError listing every problem."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise CorpusError(f"{root}: no such corpus directory")
    errors: list[str] = []
    families = _load_families(root / "families.yaml", errors)

    repos: dict[str, Repo] = {}
    raw_repos = _read_yaml(root / "repos.yaml", errors, [])
    if not isinstance(raw_repos, list):
        errors.append("repos.yaml: expected a list of repos")
        raw_repos = []
    for i, raw in enumerate(raw_repos):
        repo = _parse_repo(raw, f"repos.yaml[{i}]", errors)
        if repo is not None:
            if repo.id in repos:
                errors.append(f"repos.yaml[{i}]: duplicate repo id {repo.id!r}")
            repos[repo.id] = repo

    labels: dict[str, Label] = {}
    for path in sorted((root / "labels").glob("*.yaml")):
        raw_labels = _read_yaml(path, errors, [])
        if not isinstance(raw_labels, list):
            errors.append(f"labels/{path.name}: expected a list of labels")
            continue
        for i, raw in enumerate(raw_labels):
            where = f"labels/{path.name}[{i}]"
            label = _parse_label(raw, where, families, errors)
            if label is None:
                continue
            if label.repo != path.stem:
                errors.append(f"{where}: repo {label.repo!r} belongs in labels/{label.repo}.yaml")
            if label.repo not in repos:
                errors.append(f"{where}: unknown repo {label.repo!r}")
            if label.id in labels:
                errors.append(f"{where}: duplicate label id {label.id!r}")
            labels[label.id] = label

    suites: dict[str, Suite] = {}
    for path in sorted((root / "suites").glob("*.yaml")):
        suite = _parse_suite(path, repos, errors)
        if suite is not None:
            suites[suite.id] = suite

    if errors:
        raise CorpusError("\n".join(errors))
    return Corpus(root=root, repos=repos, families=families, labels=labels, suites=suites)


def check_code(corpus: Corpus, checkout: Callable[[Repo], Path]) -> list[str]:
    """Every label's and anchor's file and lines exist at its repo's pinned revision."""
    errors: list[str] = []
    roots: dict[str, Path | None] = {}

    def file_lines(repo_id: str, rel: str, where: str) -> list[str] | None:
        if repo_id not in roots:
            try:
                roots[repo_id] = checkout(corpus.repos[repo_id])
            except Exception as exc:  # noqa: BLE001 — report it and keep checking the other repos
                errors.append(f"repo {repo_id}: checkout failed: {exc}")
                roots[repo_id] = None
        if roots[repo_id] is None:
            return None
        path = roots[repo_id] / rel
        if not path.is_file():
            errors.append(f"{where}: {rel} does not exist at {repo_id}@{corpus.repos[repo_id].rev[:12]}")
            return None
        return path.read_text(errors="replace").splitlines()

    for label in corpus.labels.values():
        where = f"label {label.id}"
        lines = file_lines(label.repo, label.file, where)
        if lines is None:
            continue
        if label.span[1] > len(lines):
            errors.append(f"{where}: span {list(label.span)} runs past the end of {label.file} ({len(lines)} lines)")
        elif not lines[label.sink_line - 1].strip():
            errors.append(f"{where}: sink_line {label.sink_line} of {label.file} is blank")
        for rel, span in label.also:
            also_lines = file_lines(label.repo, rel, f"{where} (also)")
            if also_lines is not None and span[1] > len(also_lines):
                errors.append(f"{where}: also-span {list(span)} runs past the end of {rel} ({len(also_lines)} lines)")
    for suite in corpus.suites.values():
        for anchor in suite.anchors:
            where = f"suite {suite.id}, anchor {anchor.id}"
            lines = file_lines(anchor.repo, anchor.path, where)
            if lines is not None and anchor.region and anchor.region[1] > len(lines):
                errors.append(
                    f"{where}: region {list(anchor.region)} runs past the end of {anchor.path} ({len(lines)} lines)"
                )
    return errors


def check_public(corpus: Corpus) -> list[str]:
    """The export guard for evals/public/: public repos, and labels only from public advisories."""
    errors = [
        f"repo {repo.id}: a public corpus needs an https:// source, not {repo.source!r}"
        for repo in corpus.repos.values() if not repo.source.startswith("https://")
    ]
    errors += [
        f"label {label.id}: only public-advisory labels (source: advisory, https:// source_ref) may be public"
        for label in corpus.labels.values()
        if label.source != "advisory" or not label.source_ref.startswith("https://")
    ]
    return errors


def check_signoff(corpus: Corpus) -> list[str]:
    """Every model-only positive in a frozen suite must carry a human sign-off."""
    errors = []
    for suite in corpus.suites.values():
        if not suite.frozen:
            continue
        for anchor in suite.anchors:
            for label in corpus.labels_in_scope(anchor):
                if (label.truth == "positive" and label.source in MODEL_ONLY_SOURCES
                        and "human" not in label.reviewed_by):
                    errors.append(
                        f"suite {suite.id} is frozen but label {label.id} ({label.source}) has no human sign-off"
                    )
    return errors
