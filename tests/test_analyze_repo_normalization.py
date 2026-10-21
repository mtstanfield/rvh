"""Post-validator that catches trace-vs-output drift in analyze_repo.

Coriolix 2026-05-03 reproduced the failure mode: the model's reasoning
trace correctly identified `.github/` as CI, then the structured JSON
output put `.github/` in production_paths anyway. The normalizer fixes
these self-contradictions deterministically before the analysis is saved.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_repo import RepoAnalysis, normalize_repo_analysis  # noqa: E402


def test_github_in_production_moves_to_ci():
    """The exact coriolix failure mode."""
    analysis = RepoAnalysis(
        production_paths=[".github/", "api/", "gui/"],
        ci_paths=[],
        rationale="...",
    )
    normalized, notes = normalize_repo_analysis(analysis)
    assert ".github/" not in normalized.production_paths
    assert ".github/" in normalized.ci_paths
    assert any(".github/" in n and "ci_paths" in n for n in notes)


def test_node_modules_moves_to_generated():
    analysis = RepoAnalysis(
        production_paths=["node_modules/", "src/"],
        rationale="...",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert "node_modules/" not in normalized.production_paths
    assert "node_modules/" in normalized.generated_paths


def test_dist_build_target_all_get_classified_as_generated():
    analysis = RepoAnalysis(
        production_paths=["dist/", "build/", "target/", "out/"],
        rationale="...",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert normalized.production_paths == []
    assert set(normalized.generated_paths) == {"dist/", "build/", "target/", "out/"}


def test_tests_in_production_moves_to_test_paths():
    analysis = RepoAnalysis(
        production_paths=["tests/", "src/"],
        rationale="...",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert "tests/" not in normalized.production_paths
    assert "tests/" in normalized.test_paths


def test_already_canonical_passes_through_unchanged():
    """No notes when the input is already correct."""
    analysis = RepoAnalysis(
        production_paths=["api/", "gui/"],
        ci_paths=[".github/"],
        test_paths=["tests/"],
        generated_paths=["node_modules/"],
        rationale="...",
    )
    normalized, notes = normalize_repo_analysis(analysis)
    assert notes == []
    assert normalized.production_paths == ["api/", "gui/"]
    assert normalized.ci_paths == [".github/"]


def test_normalization_is_idempotent():
    analysis = RepoAnalysis(
        production_paths=[".github/", "api/", "node_modules/"],
        rationale="...",
    )
    once, _ = normalize_repo_analysis(analysis)
    twice, second_notes = normalize_repo_analysis(once)
    assert second_notes == []
    assert twice.to_dict() == once.to_dict()


def test_rationale_preserved():
    analysis = RepoAnalysis(
        production_paths=[".github/", "api/"],
        rationale="My rationale text.",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert normalized.rationale == "My rationale text."


def test_no_double_insert_when_already_in_target_bucket():
    """If `.github/` is in BOTH production_paths and ci_paths, drop the
    duplicate from production_paths but don't double-insert into ci_paths."""
    analysis = RepoAnalysis(
        production_paths=[".github/", "api/"],
        ci_paths=[".github/"],
        rationale="...",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert normalized.ci_paths.count(".github/") == 1
    assert ".github/" not in normalized.production_paths


def test_paths_not_in_canonical_list_unaffected():
    """A custom directory like `web/static/` should not be touched."""
    analysis = RepoAnalysis(
        production_paths=["web/static/", "api/"],
        rationale="...",
    )
    normalized, _ = normalize_repo_analysis(analysis)
    assert "web/static/" in normalized.production_paths
