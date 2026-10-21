"""The rvh command-line surface (run_repo_vuln_hunt.build_parser)."""
import pytest

import run_repo_vuln_hunt as r

REQUIRED = ["--repo", "x", "--endpoint", "http://e", "--model", "m"]


def test_defaults():
    a = r.build_parser().parse_args(REQUIRED)
    assert (a.parallelism, a.min_priority, a.max_tasks, a.api_key) == (1, 3, None, None)
    assert (a.include, a.exclude, a.include_vendored, a.no_cpg, a.fresh) == ([], [], False, False, False)


@pytest.mark.parametrize("missing", ["--repo", "--endpoint", "--model"])
def test_repo_endpoint_and_model_are_required(missing):
    argv = dict(zip(REQUIRED[::2], REQUIRED[1::2]))
    argv.pop(missing)
    with pytest.raises(SystemExit):
        r.build_parser().parse_args([tok for pair in argv.items() for tok in pair])


def test_min_priority_must_be_a_score():
    with pytest.raises(SystemExit):
        r.build_parser().parse_args([*REQUIRED, "--min-priority", "6"])


def test_exclude_splits_globs_from_paths():
    assert r.split_excludes(["vendor", "*/gen/*", "a[bc].py", "docs/", "x?.c"]) == (
        ["vendor", "docs/"], ["*/gen/*", "a[bc].py", "x?.c"],
    )


def _stop_at_build(monkeypatch):
    seen = {}

    def fake_build_cpg(**kwargs):
        seen.update(kwargs)
        raise r.CpgBuildError("stop here")

    monkeypatch.setattr(r, "build_cpg", fake_build_cpg)
    return seen


def test_cpg_phase_builds_into_a_given_dir(tmp_path, monkeypatch):
    seen = _stop_at_build(monkeypatch)
    out = r.phase_cpg_build_and_sidecar(repo_path=tmp_path, run_dir=tmp_path / "run", lang="python",
                                        prefix="p", network="n", cpg_dir=tmp_path / "cache" / "cpg")
    assert out is None and seen["cpg_dir"] == tmp_path / "cache" / "cpg"


def test_cpg_phase_defaults_to_the_run_dir(tmp_path, monkeypatch):
    seen = _stop_at_build(monkeypatch)
    r.phase_cpg_build_and_sidecar(repo_path=tmp_path, run_dir=tmp_path / "run", lang="python",
                                  prefix="p", network="n")
    assert seen["cpg_dir"] == tmp_path / "run" / "cpg"
