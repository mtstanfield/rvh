"""Finding-to-label matching (scripts/eval_match.py)."""
import pytest

from eval_corpus import Label
from eval_match import Finding, Loc, match_levels, parse_locations, path_matches
from tests.evalkit import label


def _label(**overrides) -> Label:
    raw = label(**overrides)
    also = [(a["file"], tuple(a["span"])) for a in raw.pop("also", [])]
    return Label(**{**raw, "span": tuple(raw["span"]), "also": also})


def _finding(where: str, family: str | None, anchor: str = "app.py") -> Finding:
    return Finding(task_id=1, title="t", cwe=None, family=family, anchor_file=anchor,
                   locs=parse_locations(where, anchor))


@pytest.mark.parametrize("text, expected", [
    ("gui/views.py:4640", [Loc("gui/views.py", 4640, 4640)]),
    ("/workspace/app/Model/X.php:55", [Loc("app/Model/X.php", 55, 55)]),
    ("gui/views.py:4640 (entry.delete()); def at gui/views.py:4631",
     [Loc("gui/views.py", 4640, 4640), Loc("gui/views.py", 4631, 4631)]),
    ("CPUGraph.tsx:123-125 (getData); at prometheus.tsx:355-363",
     [Loc("CPUGraph.tsx", 123, 125), Loc("prometheus.tsx", 355, 363)]),
    ("view_content.tpl:68,72", [Loc("view_content.tpl", 68, 68), Loc("view_content.tpl", 72, 72)]),
    ("validate:205-208 -> api_connect.py:501", [Loc("validate", 205, 208), Loc("api_connect.py", 501, 501)]),
    ("src/x.go:L42", [Loc("src/x.go", 42, 42)]),
    ("timeout 10:30 then app.py:7", [Loc("app.py", 7, 7)]),
    ("the handler at line 998", [Loc("anchor.py", 998, 998)]),
    ("lines 990-1005", [Loc("anchor.py", 990, 1005)]),
    ("no location given", []),
])
def test_parse_locations(text, expected):
    assert parse_locations(text, "/workspace/anchor.py") == expected


def test_path_matching_allows_shortened_paths_at_a_boundary():
    assert path_matches("gui/views.py", "gui/views.py")
    assert path_matches("gui/views.py", "views.py")
    assert not path_matches("gui/views.py", "iews.py")
    assert not path_matches("gui/views.py", "api/views.py")


def test_match_levels():
    lab = _label()  # app.py:3, span [2, 4], code-injection
    assert match_levels(lab, [_finding("app.py:14", "code-injection")]) == \
        {"file": True, "location": True, "location_cwe": True}  # 4 + 10 lines of slack
    assert match_levels(lab, [_finding("app.py:15", "code-injection")]) == \
        {"file": True, "location": False, "location_cwe": False}
    assert match_levels(lab, [_finding("app.py:3", "xss")]) == \
        {"file": True, "location": True, "location_cwe": False}
    assert match_levels(lab, [_finding("other.py:3", "code-injection")]) == \
        {"file": False, "location": False, "location_cwe": False}
    assert match_levels(lab, []) == {"file": False, "location": False, "location_cwe": False}


def test_a_range_overlapping_the_span_matches():
    assert match_levels(_label(sink_line=50, span=[50, 50]), [_finding("app.py:30-45", "code-injection")])["location"]


def test_with_no_location_the_anchor_file_counts_at_file_level():
    finding = Finding(task_id=1, title="t", cwe=None, family=None, anchor_file="/workspace/app.py", locs=[])
    assert match_levels(_label(), [finding]) == {"file": True, "location": False, "location_cwe": False}


def test_a_label_matches_at_any_of_its_extra_locations():
    """Diffuse bugs (mass assignment, missing authz) have several fair sink lines."""
    lab = _label(sink_line=132, span=[132, 138], also=[{"file": "app.py", "span": [20, 20]},
                                                        {"file": "models/token.py", "span": [88, 90]}])
    assert match_levels(lab, [_finding("app.py:20", "code-injection")])["location"]
    assert match_levels(lab, [_finding("models/token.py:89", "code-injection", anchor="models/token.py")])["location"]
    assert not match_levels(lab, [_finding("app.py:60", "code-injection")])["location"]
