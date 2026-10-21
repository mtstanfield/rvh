"""Deterministic matching of worker findings to labels.

A finding's ``sink_location`` is free text from the model — usually
``path:line``, but also ``/workspace/path:line``, bare file names, ranges
(``123-125``), lists (``68,72``), several locations joined by prose, or just
``line 998``. ``parse_locations`` pulls out every location it can; a label
then matches at three levels:

* file          — a location (or, with none, the anchor file) is the label's file
* location      — a location overlaps the label's span ± SPAN_SLACK lines
* location_cwe  — location, and the finding's CWE is in the label's family
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from eval_corpus import Label

SPAN_SLACK = 10
LEVELS = ("file", "location", "location_cwe")

_PATH_LINES = re.compile(r"(?P<path>[A-Za-z0-9_./-]+):L?(?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)")
_BARE_LINES = re.compile(r"\b(?:lines?\s*|L)(?P<lines>\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*)", re.IGNORECASE)
_WORKSPACE = "/workspace/"


@dataclass(frozen=True)
class Loc:
    path: str
    start: int
    end: int


@dataclass
class Finding:
    task_id: int
    title: str
    cwe: str | None
    family: str | None
    anchor_file: str
    locs: list[Loc]
    summary: str = ""


def normalize_path(path: str) -> str:
    """The repo-relative form of a path a worker wrote (no /workspace/, ./, or leading /)."""
    if path.startswith(_WORKSPACE):
        path = path[len(_WORKSPACE):]
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _ranges(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.replace(" ", "").split(","):
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        out.append((min(a, b), max(a, b)))
    return out


def parse_locations(text: str, anchor_file: str) -> list[Loc]:
    """Every location in a ``sink_location``; bare line numbers refer to the anchor file."""
    locs: list[Loc] = []
    for m in _PATH_LINES.finditer(text):
        path = normalize_path(m["path"])
        if re.search(r"[A-Za-z]", path):  # "10:30" is a time, not a location
            locs += [Loc(path, a, b) for a, b in _ranges(m["lines"])]
    if not locs:
        for m in _BARE_LINES.finditer(text):
            locs += [Loc(normalize_path(anchor_file), a, b) for a, b in _ranges(m["lines"])]
    return locs


def path_matches(label_file: str, path: str) -> bool:
    """Same file, allowing a worker's shortened path (``views.py`` for ``gui/views.py``)."""
    return path == label_file or label_file.endswith("/" + path)


def distance(label: Label, finding: Finding) -> int | None:
    """Lines between the finding's nearest location and the label's span (0 = inside);
    None when no location is in the label's file within SPAN_SLACK."""
    gaps = [
        max(span[0] - loc.end, loc.start - span[1], 0)
        for file, span in label.locations
        for loc in finding.locs if path_matches(file, loc.path)
    ]
    gaps = [gap for gap in gaps if gap <= SPAN_SLACK]
    return min(gaps) if gaps else None


def at_location(label: Label, finding: Finding) -> bool:
    return distance(label, finding) is not None


def in_file(label: Label, finding: Finding) -> bool:
    paths = [loc.path for loc in finding.locs] or [normalize_path(finding.anchor_file)]
    return any(path_matches(file, path) for file, _ in label.locations for path in paths)


def match_levels(label: Label, findings: list[Finding]) -> dict[str, bool]:
    """Whether any of ``findings`` matches ``label`` at each level."""
    return {
        "file": any(in_file(label, f) for f in findings),
        "location": any(at_location(label, f) for f in findings),
        "location_cwe": any(at_location(label, f) and f.family == label.family for f in findings),
    }
