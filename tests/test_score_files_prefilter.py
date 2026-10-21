"""Tests for the deterministic pre-filter in scripts/score_files.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import score_files  # noqa: E402


def _row(**overrides):
    """Build an inventory row dict with sensible defaults."""
    base = {
        "path": "src/main.go",
        "extension": ".go",
        "category": "code",
        "is_binary": 0,
        "size_bytes": 1024,
    }
    base.update(overrides)
    return base


def test_cold_matcher_matches_prefix():
    profile = {"cold_path_prefixes": ["tests/", "docs/"]}
    matcher = score_files.build_cold_prefix_matcher(profile)
    assert matcher("tests/integration/test_x.py") == "tests/"
    assert matcher("docs/api.md") == "docs/"
    assert matcher("src/main.go") is None


def test_cold_matcher_lowercases_path():
    profile = {"cold_path_prefixes": ["tests/"]}
    matcher = score_files.build_cold_prefix_matcher(profile)
    assert matcher("Tests/foo.py") == "tests/"


def test_cold_matcher_longest_prefix_wins():
    """If both 'tests/' and 'tests/integration/' match, longest wins so the
    rule-name in signals_json is informative."""
    profile = {"cold_path_prefixes": ["tests/", "tests/integration/"]}
    matcher = score_files.build_cold_prefix_matcher(profile)
    assert matcher("tests/integration/test_x.py") == "tests/integration/"


def test_cold_matcher_empty_profile():
    matcher = score_files.build_cold_prefix_matcher({})
    assert matcher("anything") is None


def test_prefilter_cold_prefix_fires():
    matcher = score_files.build_cold_prefix_matcher({"cold_path_prefixes": ["tests/"]})
    result = score_files.prefilter_file(_row(path="tests/test_x.py"), matcher)
    assert result is not None
    assert result.score == 1
    assert result.scored_by == "prefilter"
    assert result.signals == {"source": "prefilter", "rule": "cold-prefix:tests/"}


def test_prefilter_category_fires_for_docs():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row(path="src/README.md", category="docs"), matcher)
    assert result is not None
    assert result.score == 1
    assert result.signals == {"source": "prefilter", "rule": "category:docs"}


def test_prefilter_category_fires_for_asset():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row(category="asset"), matcher)
    assert result is not None
    assert result.signals["rule"] == "category:asset"


def test_prefilter_category_fires_for_metadata():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row(category="metadata"), matcher)
    assert result is not None
    assert result.signals["rule"] == "category:metadata"


def test_prefilter_non_text_extension_fires():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row(extension=".bin", category="other"), matcher)
    assert result is not None
    assert result.signals["rule"] == "non-text"


def test_prefilter_binary_fires():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row(extension=".png", is_binary=1), matcher)
    assert result is not None
    assert result.signals["rule"] == "non-text"


def test_prefilter_production_code_does_not_fire():
    matcher = score_files.build_cold_prefix_matcher({"cold_path_prefixes": ["tests/"]})
    result = score_files.prefilter_file(_row(path="src/auth/login.go"), matcher)
    assert result is None


def test_prefilter_cold_prefix_takes_priority_over_category():
    """A docs file already covered by a cold prefix should report cold-prefix
    as the rule (cold-prefix is checked first)."""
    matcher = score_files.build_cold_prefix_matcher({"cold_path_prefixes": ["docs/"]})
    result = score_files.prefilter_file(_row(path="docs/api.md", category="docs"), matcher)
    assert result is not None
    assert result.signals["rule"] == "cold-prefix:docs/"


def test_cold_matcher_matches_mixed_case_prefix():
    """The layout analysis returns repo paths as written, e.g. `ThirdParty/`."""
    matcher = score_files.build_cold_prefix_matcher({"cold_path_prefixes": ["ThirdParty/"]})
    assert matcher("ThirdParty/zlib/inflate.c") == "ThirdParty/"
