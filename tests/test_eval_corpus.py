"""The eval corpus model and loader (scripts/eval_corpus.py)."""
import pytest

from eval_corpus import CorpusError, load_corpus
from tests.evalkit import anchor, label, write_corpus

SHA = "a" * 40


def _repo(**overrides):
    return {"id": "demo", "source": "https://example.com/demo.git", "commit": SHA, **overrides}


def test_loads_a_valid_corpus(tmp_path):
    root = write_corpus(tmp_path, repos=[_repo(framework_hints=["flask"])], labels=[label()],
                        suites={"quick": {"kind": "quick", "anchors": [anchor()]}})
    corpus = load_corpus(root)
    assert corpus.repos["demo"].rev == SHA
    assert corpus.repos["demo"].framework_hints == ["flask"]
    assert corpus.labels["demo-eval-1"].span == (2, 4)
    suite = corpus.suites["quick"]
    assert (suite.kind, suite.repeats, suite.frozen, len(suite.anchors)) == ("quick", 1, False, 1)
    assert suite.anchors[0].priority == 5
    assert len(suite.sha256) == 64
    assert [lab.id for lab in corpus.labels_in_scope(suite.anchors[0])] == ["demo-eval-1"]


def test_family_of_maps_listed_cwes_and_passes_others_through(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path, repos=[_repo()]))
    assert corpus.family_of("CWE-95") == "code-injection"
    assert corpus.family_of("CWE-1234") == "CWE-1234"
    assert corpus.family_of(None) is None


def test_every_problem_is_reported_at_once(tmp_path):
    root = write_corpus(tmp_path, repos=[_repo()], labels=[
        label(id="bad-1", sink_line=9),
        label(id="bad-2", severity="huge"),
        label(id="bad-3", family="xss"),
        label(id="bad-4", repo="nope"),
    ])
    with pytest.raises(CorpusError) as exc:
        load_corpus(root)
    message = str(exc.value)
    assert "sink_line 9 is outside span [2, 4]" in message
    assert "'severity' must be one of" in message
    assert "family must be 'code-injection' for CWE-95" in message
    assert "unknown repo 'nope'" in message


@pytest.mark.parametrize("pins, message", [
    ({"commit": SHA, "tree": SHA}, "exactly one of 'commit' or 'tree'"),
    ({}, "exactly one of 'commit' or 'tree'"),
    ({"commit": "abc123"}, "full 40-character git hash"),
])
def test_a_repo_has_exactly_one_full_pin(tmp_path, pins, message):
    root = write_corpus(tmp_path, repos=[{"id": "demo", "source": "https://example.com/demo.git", **pins}])
    with pytest.raises(CorpusError, match=message):
        load_corpus(root)


def test_a_cwe_in_two_families_is_an_error(tmp_path):
    root = write_corpus(tmp_path, repos=[_repo()], families={"a": ["CWE-78"], "b": ["CWE-78"]})
    with pytest.raises(CorpusError, match="CWE-78 is in both 'a' and 'b'"):
        load_corpus(root)


def test_duplicate_label_ids_and_unknown_suite_repos_are_errors(tmp_path):
    root = write_corpus(tmp_path, repos=[_repo()], labels=[label(), label()],
                        suites={"s": {"kind": "quick", "anchors": [anchor(repo="ghost")]}})
    with pytest.raises(CorpusError) as exc:
        load_corpus(root)
    assert "duplicate label id 'demo-eval-1'" in str(exc.value)
    assert "unknown repo 'ghost'" in str(exc.value)


def test_a_region_scopes_labels_by_sink_line(tmp_path):
    corpus = load_corpus(write_corpus(
        tmp_path, repos=[_repo()],
        labels=[label(id="in", sink_line=120, span=[118, 122]), label(id="out", sink_line=300, span=[300, 301])],
        suites={"s": {"kind": "full", "anchors": [anchor(region=[100, 200])]}},
    ))
    assert [lab.id for lab in corpus.labels_in_scope(corpus.suites["s"].anchors[0])] == ["in"]


def test_by_repo_keeps_suite_order(tmp_path):
    corpus = load_corpus(write_corpus(
        tmp_path, repos=[_repo(), _repo(id="other")],
        suites={"s": {"kind": "full", "anchors": [
            anchor(id="a"), anchor(id="b", repo="other"), anchor(id="c", path="lib.py"),
        ]}},
    ))
    grouped = corpus.suites["s"].by_repo()
    assert list(grouped) == ["demo", "other"]
    assert [a.id for a in grouped["demo"]] == ["a", "c"]


from eval_corpus import check_code, check_public, check_signoff  # noqa: E402
from tests.evalkit import APP_PY, make_git_repo  # noqa: E402


def test_check_code_flags_missing_files_short_files_and_blank_sinks(tmp_path):
    src = tmp_path / "demo"
    (sha,) = make_git_repo(src, {"app.py": APP_PY, "blank.py": "x = 1\n\n\n"})
    corpus = load_corpus(write_corpus(
        tmp_path / "corpus", repos=[_repo(source=str(src), commit=sha)],
        labels=[
            label(id="ok"),
            label(id="gone", file="missing.py"),
            label(id="long", span=[2, 40]),
            label(id="blank", file="blank.py", sink_line=2, span=[1, 3]),
        ],
        suites={"s": {"kind": "quick", "anchors": [anchor(), anchor(id="big", region=[1, 99])]}},
    ))
    errors = check_code(corpus, lambda repo: src)
    assert any("label gone: missing.py does not exist" in e for e in errors)
    assert any("label long: span [2, 40] runs past the end of app.py (4 lines)" in e for e in errors)
    assert any("label blank: sink_line 2 of blank.py is blank" in e for e in errors)
    assert any("anchor big: region [1, 99] runs past the end" in e for e in errors)
    assert len(errors) == 4


def test_check_code_reports_a_failed_checkout_once(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path, repos=[_repo()], labels=[label(), label(id="two")]))

    def unreachable(repo):
        raise RuntimeError("no network")

    assert check_code(corpus, unreachable) == ["repo demo: checkout failed: no network"]


def test_check_public_allows_only_public_advisories_on_public_repos(tmp_path):
    ok = load_corpus(write_corpus(
        tmp_path / "ok", repos=[_repo()],
        labels=[label(source="advisory", source_ref="https://github.com/advisories/GHSA-0000-0000-0000")],
    ))
    assert check_public(ok) == []
    bad = load_corpus(write_corpus(tmp_path / "bad", repos=[_repo(source="/home/me/private")], labels=[label()]))
    errors = check_public(bad)
    assert len(errors) == 2
    assert "needs an https:// source" in errors[0]
    assert "only public-advisory labels" in errors[1]


def test_check_signoff_applies_only_to_frozen_suites(tmp_path):
    labels = [
        label(source="claude-review"),
        label(id="signed", sink_line=4, source="claude-review", reviewed_by=["claude-code", "human"]),
    ]
    draft = load_corpus(write_corpus(tmp_path / "d", repos=[_repo()], labels=labels,
                                     suites={"s": {"kind": "full", "anchors": [anchor()]}}))
    assert check_signoff(draft) == []
    frozen = load_corpus(write_corpus(tmp_path / "f", repos=[_repo()], labels=labels,
                                      suites={"s": {"kind": "full", "frozen": True, "anchors": [anchor()]}}))
    assert check_signoff(frozen) == [
        "suite s is frozen but label demo-eval-1 (claude-review) has no human sign-off",
    ]


def test_extra_locations_are_parsed_and_checked(tmp_path):
    corpus = load_corpus(write_corpus(tmp_path / "ok", repos=[_repo()],
                                      labels=[label(also=[{"file": "lib.py", "span": [5, 7]}])]))
    assert corpus.labels["demo-eval-1"].also == [("lib.py", (5, 7))]
    with pytest.raises(CorpusError, match="'also'"):
        load_corpus(write_corpus(tmp_path / "bad", repos=[_repo()], labels=[label(also=[{"file": "lib.py"}])]))
