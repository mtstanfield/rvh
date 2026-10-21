"""Paired comparison of two evals (scripts/eval_compare.py)."""
import pytest

from eval_compare import compare, paired_bootstrap, render_compare_md


def _summary(eval_id, found, *, kind="full", family="code-injection", sha="s"):
    return {
        "eval_id": eval_id, "suite": "full-v1", "suite_kind": kind, "suite_sha256": sha,
        "labels": {lid: {"family": family,
                         "runs": [{"file": f, "location": f, "location_cwe": f} for f in runs]}
                   for lid, runs in found.items()},
        "metrics": {"cost": {"tasks": 0}},
    }


def test_identical_evals_show_no_difference():
    runs = {f"l{i}": [i % 2 == 0, i % 3 == 0, True] for i in range(30)}
    result = compare(_summary("a", runs), _summary("b", runs))
    assert result["overall"]["diff"] == 0
    assert result["overall"]["ci95"] == [0.0, 0.0]
    assert not result["overall"]["significant"]
    assert result["same_suite"] and not result["smoke"]


def test_a_large_consistent_gain_is_significant():
    a = {f"l{i}": [False, False, False] for i in range(30)}
    b = {f"l{i}": [i < 20] * 3 for i in range(30)}
    result = compare(_summary("a", a), _summary("b", b))
    assert result["overall"]["diff"] == pytest.approx(20 / 30)
    assert result["overall"]["significant"] and result["overall"]["ci95"][0] > 0
    assert len(result["gained"]) == 20 and result["lost"] == []


def test_one_flipped_label_is_noise():
    a = {f"l{i}": [i % 2 == 0] for i in range(30)}
    b = dict(a, l1=[True])
    assert not compare(_summary("a", a), _summary("b", b))["overall"]["significant"]


def test_only_shared_labels_are_paired():
    result = compare(_summary("a", {"x": [True], "y": [True]}), _summary("b", {"y": [False], "z": [True]}))
    assert result["overall"]["n"] == 1
    assert (result["only_a"], result["only_b"], result["lost"]) == (["x"], ["z"], ["y"])


def test_quick_or_mismatched_suites_are_flagged():
    runs = {"x": [True]}
    result = compare(_summary("a", runs, kind="quick"), _summary("b", runs, sha="other"))
    assert result["smoke"] and not result["same_suite"]


def test_the_bootstrap_is_deterministic():
    diffs = [0.0, 1.0, -1.0, 0.5] * 5
    assert paired_bootstrap(diffs) == paired_bootstrap(diffs)


def test_the_markdown_states_the_verdict():
    runs = {f"l{i}": [True] for i in range(5)}
    md = render_compare_md(compare(_summary("a", runs, kind="quick"), _summary("b", runs, kind="quick")))
    assert "no significant difference" in md
    assert "Smoke — not a decision" in md


def test_the_cost_table_counts_unoffered_tool_calls():
    runs = {f"l{i}": [True] for i in range(5)}

    def with_cost(summary, unoffered):
        summary["metrics"]["cost"] = {"tasks": 5, "wall_median_s": 300.0, "input_tokens": 10, "output_tokens": 1,
                                      "unadvertised_tools": unoffered}
        return summary

    md = render_compare_md(compare(with_cost(_summary("a", runs), {"read_file": 3, "todo_todoWrite": 2}),
                                   with_cost(_summary("b", runs), {})))
    assert "| calls to unoffered tools | 5 | 0 |" in md


def test_a_family_with_too_few_labels_is_never_significant():
    """One label flipping gives a degenerate bootstrap CI ([+100%, +100%]); that isn't evidence."""
    a = _summary("a", {"x": [False]}, family="xss")
    b = _summary("b", {"x": [True]}, family="xss")
    result = compare(a, b)
    assert result["by_family"]["xss"]["ci95"] == [1.0, 1.0]
    assert not result["by_family"]["xss"]["significant"]
    assert not result["overall"]["significant"]
