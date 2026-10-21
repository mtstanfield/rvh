"""Lenient-parse behaviors covering format drift seen in the qwen36 worker:
bare scalars in frontmatter (`severity: high`) and blended severity values
(`severity: "medium-high"`). Quoted-string canonical paths are unaffected."""

import pytest

from report_utils import parse_frontmatter, parse_scalar


def _wrap(frontmatter_body: str) -> str:
    return f"---\n{frontmatter_body}\n---\n\n# Summary\n\nbody.\n"


# --- bare-scalar promotion ----------------------------------------------------


def test_bare_severity_token_promoted_to_string():
    text = _wrap(
        'verdict: "finding"\n'
        'finding_type: "SQL injection"\n'
        "severity: high\n"
        'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["severity"] == "high"


def test_bare_cwe_token_promoted_to_string():
    text = _wrap(
        'verdict: "finding"\n'
        'finding_type: "SQL injection"\n'
        'severity: "high"\n'
        'title: "x"\n'
        "cwe: CWE-89\n"
    )
    md, _ = parse_frontmatter(text)
    assert md["cwe"] == "CWE-89"


def test_bare_finding_type_with_underscores_promoted():
    text = _wrap(
        'verdict: "finding"\n'
        "finding_type: sql_injection\n"
        'severity: "high"\n'
        'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["finding_type"] == "sql_injection"


def test_bare_scalar_with_space_still_rejected():
    """Multi-word values must be quoted — protects against truncation bugs."""
    with pytest.raises(ValueError, match="frontmatter values must be quoted"):
        parse_scalar("Dynamic Model Lookup")


def test_bare_scalar_with_special_chars_still_rejected():
    with pytest.raises(ValueError, match="frontmatter values must be quoted"):
        parse_scalar("foo,bar")


# --- severity blend normalization --------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("medium-high", "high"),
        ("high-medium", "high"),
        ("low-medium", "medium"),
        ("medium-low", "medium"),
        ("low-high", "high"),
        ("medium/high", "high"),
        ("Medium-High", "high"),
        ("medium - high", "high"),
    ],
)
def test_blended_severity_normalized_to_higher(raw, expected):
    text = _wrap(
        f'verdict: "finding"\n'
        f'finding_type: "x"\n'
        f'severity: "{raw}"\n'
        f'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["severity"] == expected


def test_unrecognized_severity_blend_still_rejected():
    text = _wrap(
        'verdict: "finding"\n'
        'finding_type: "x"\n'
        'severity: "spicy-medium"\n'
        'title: "x"\n'
    )
    with pytest.raises(ValueError, match="severity must be one of"):
        parse_frontmatter(text)


def test_canonical_severity_unchanged():
    text = _wrap(
        'verdict: "no-finding"\n'
        'finding_type: "none"\n'
        'severity: "info"\n'
        'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["severity"] == "info"


# --- regression: real failing reports from the coriolix overnight run --------


def test_task_61_finding_severity_medium_high_recovers():
    """Reproduces frontmatter an earlier worker emitted with
    severity: \"medium-high\"."""
    text = (
        "---\n"
        'verdict: "finding"\n'
        'finding_type: "Unauthenticated file upload via missing decorator"\n'
        'cwe: ""\n'
        'severity: "medium-high"\n'
        'title: "Missing @ on login_required"\n'
        "dynamic_evidence: false\n"
        "task_id: 61\n"
        'anchor_file: "/workspace/gui/views.py"\n'
        'confidence: "high"\n'
        "---\n\n# Summary\n\nbody.\n"
    )
    md, _ = parse_frontmatter(text)
    assert md["severity"] == "high"
    assert md["task_id"] == 61


def test_task_106_finding_bare_scalars_recovers():
    """Reproduces frontmatter with bare scalars (severity, cwe, finding_type,
    sink_location)."""
    text = (
        "---\n"
        'title: "SQL injection via subprocess output in rename_keys.py"\n'
        "severity: high\n"
        "finding_type: sql_injection\n"
        "cwe: CWE-89\n"
        "anchor_file: /workspace/gui/scripts/rename_keys.py\n"
        "sink_location: gui/scripts/rename_keys.py:150\n"
        "task_id: 106\n"
        'verdict: "finding"\n'
        "---\n\n# Summary\n\nbody.\n"
    )
    md, _ = parse_frontmatter(text)
    assert md["severity"] == "high"
    assert md["cwe"] == "CWE-89"
    assert md["finding_type"] == "sql_injection"
    assert md["anchor_file"] == "/workspace/gui/scripts/rename_keys.py"
    assert md["sink_location"] == "gui/scripts/rename_keys.py:150"


# --- sink_location / sink_category as accepted optional fields ---------------


def test_sink_location_quoted_accepted():
    text = _wrap(
        'verdict: "finding"\n'
        'finding_type: "x"\n'
        'severity: "high"\n'
        'title: "x"\n'
        'sink_location: "gui/views.py:1968"\n'
        'sink_category: "shell"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["sink_location"] == "gui/views.py:1968"
    assert md["sink_category"] == "shell"


def test_sink_location_must_be_non_empty_string():
    text = _wrap(
        'verdict: "finding"\n'
        'finding_type: "x"\n'
        'severity: "high"\n'
        'title: "x"\n'
        'sink_location: ""\n'
    )
    with pytest.raises(ValueError, match="sink_location must be a non-empty"):
        parse_frontmatter(text)


# --- verdict inference when omitted ------------------------------------------


def test_missing_verdict_with_finding_type_inferred_finding():
    text = _wrap(
        'finding_type: "SQL injection"\n'
        'severity: "high"\n'
        'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["verdict"] == "finding"


def test_missing_verdict_with_finding_type_none_inferred_nofinding():
    text = _wrap(
        'finding_type: "none"\n'
        'severity: "info"\n'
        'title: "x"\n'
    )
    md, _ = parse_frontmatter(text)
    assert md["verdict"] == "no-finding"


def test_missing_verdict_and_missing_finding_type_still_fails():
    text = _wrap(
        'severity: "info"\n'
        'title: "x"\n'
    )
    with pytest.raises(ValueError, match="missing required fields"):
        parse_frontmatter(text)
