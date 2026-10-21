"""rvh eval's command line (scripts/eval_cli.py)."""
import json
from datetime import datetime, timezone

import pytest
import yaml

import eval_cli
from tests.evalkit import APP_PY, anchor, label, make_git_repo, write_corpus


def build_corpus(tmp_path, labels=None):
    (sha,) = make_git_repo(tmp_path / "demo", {"app.py": APP_PY})
    return write_corpus(
        tmp_path / "corpus",
        repos=[{"id": "demo", "source": str(tmp_path / "demo"), "commit": sha, "framework_hints": ["flask"]}],
        labels=[label()] if labels is None else labels,
        suites={"quick": {"kind": "quick", "repeats": 2, "anchors": [anchor()]}},
    )


def test_bare_flags_mean_run():
    assert eval_cli.normalize_argv(["--corpus", "c"]) == ["run", "--corpus", "c"]
    assert eval_cli.normalize_argv(["compare", "a", "b"]) == ["compare", "a", "b"]
    assert eval_cli.normalize_argv(["--help"]) == ["--help"]


def test_eval_ids_are_time_suite_model_and_label():
    now = datetime(2026, 9, 24, 13, 5, tzinfo=timezone.utc)
    assert eval_cli.make_eval_id("quick", "qwen38-27b", None, now) == "20260924-1305-quick-qwen38-27b"
    assert eval_cli.make_eval_id("full-v1", "org/Model:Q4", "no cpg", now) == \
        "20260924-1305-full-v1-org-Model-Q4-no-cpg"


def test_unique_dir_never_reuses_a_name(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x-2").mkdir()
    assert eval_cli.unique_dir(tmp_path, "x") == tmp_path / "x-3"
    assert eval_cli.unique_dir(tmp_path, "y") == tmp_path / "y"


def test_validate_passes_a_clean_corpus(tmp_path, capsys):
    eval_cli.main(["validate", "--corpus", str(build_corpus(tmp_path))])
    assert "ok: 1 repo(s), 1 positive and 0 negative label(s), 1 suite(s)" in capsys.readouterr().out


def test_validate_checks_labels_against_the_pinned_code(tmp_path, capsys):
    root = build_corpus(tmp_path, labels=[label(span=[2, 40])])
    with pytest.raises(SystemExit, match="1 problem"):
        eval_cli.main(["validate", "--corpus", str(root)])
    assert "runs past the end of app.py" in capsys.readouterr().out


def test_validate_public_rejects_private_labels(tmp_path, capsys):
    with pytest.raises(SystemExit):
        eval_cli.main(["validate", "--corpus", str(build_corpus(tmp_path)), "--public", "--no-code"])
    out = capsys.readouterr().out
    assert "needs an https:// source" in out and "only public-advisory labels" in out


def test_a_malformed_corpus_exits_with_every_problem(tmp_path):
    root = build_corpus(tmp_path, labels=[label(severity="huge")])
    with pytest.raises(SystemExit, match="'severity' must be one of"):
        eval_cli.main(["validate", "--corpus", str(root)])


def test_pin_prints_a_repos_entry(tmp_path, capsys):
    (sha,) = make_git_repo(tmp_path / "demo", {"app.py": APP_PY})
    eval_cli.main(["pin", str(tmp_path / "demo")])
    (entry,) = yaml.safe_load(capsys.readouterr().out)
    assert (entry["commit"], entry["source"], entry["framework_hints"]) == (sha, str(tmp_path / "demo"), [])


def test_compare_reads_results_dirs(tmp_path, capsys):
    summary = {"suite": "full-v1", "suite_kind": "full", "suite_sha256": "s", "metrics": {"cost": {"tasks": 0}},
               "labels": {f"l{i}": {"family": "xss", "runs": [
                   {"file": True, "location": i % 2 == 0, "location_cwe": False}]} for i in range(10)}}
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text(json.dumps({**summary, "eval_id": name}))
    eval_cli.main(["compare", str(tmp_path / "a"), str(tmp_path / "b"), "--out", str(tmp_path / "cmp.md")])
    assert "no significant difference" in capsys.readouterr().out
    assert "no significant difference" in (tmp_path / "cmp.md").read_text()


from common import connect_db  # noqa: E402


def fake_dispatch(calls):
    """Stands in for Docker + the engine: every pending task reports eval() at app.py:3."""
    def dispatch(*, repo, run_dir, cpg, **_):
        calls.append((repo.id, cpg))
        with connect_db(run_dir) as conn:
            for (task_id,) in conn.execute("SELECT id FROM tasks WHERE status = 'pending'").fetchall():
                report_id = conn.execute(
                    "INSERT INTO worker_reports (task_id, report_path, title, verdict, severity, confidence,"
                    " metadata_json, created_at) VALUES (?, 'r.md', 't', 'finding', 'critical', 'high', '{}', 'now')",
                    (task_id,),
                ).lastrowid
                conn.execute(
                    "INSERT INTO findings (task_id, report_id, title, finding_type, severity, confidence, status,"
                    " anchor_file, related_files_json, summary, cwe, created_at) VALUES (?, ?, 'eval of input',"
                    " 'code-injection', 'critical', 'high', 'candidate', '/workspace/app.py', ?, 's', 'CWE-95', 'now')",
                    (task_id, report_id, json.dumps(["/workspace/app.py:3"])),
                )
                conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
        return {"cpg": "ok" if cpg else "off", "engine_exit": 0}
    return dispatch


@pytest.fixture
def offline(monkeypatch):
    """No endpoint, Docker, or image build; returns the fake dispatch's call log."""
    monkeypatch.setattr(eval_cli, "run_preflight", lambda *a, **k: None)
    monkeypatch.setattr(eval_cli, "ensure_worker_image", lambda log: "rvh-worker:test")
    monkeypatch.setattr(eval_cli, "get_joern_version", lambda: "joern@test")
    monkeypatch.setattr(eval_cli, "start_caffeinate", lambda: None)
    monkeypatch.setenv("RVH_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(eval_cli, "dispatch_repo", fake_dispatch(calls))
    return calls


RUN = ["--suite", "quick", "--endpoint", "http://llm:8000", "--model", "m"]


def test_run_seeds_dispatches_and_scores(tmp_path, offline):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN, "--no-cpg"])
    (results,) = (root / "results").iterdir()
    summary = json.loads((results / "summary.json").read_text())
    assert offline == [("demo", False)]
    assert summary["metrics"]["recall"]["location_cwe"]["all_runs"] == 1.0
    assert summary["metrics"]["tasks"] == {"seeded": 2, "finished": 2}
    prov = summary["provenance"]
    assert prov["complete"] and prov["finished_at"]
    assert prov["arm"] == {"cpg": False, "parallelism": 1, "repeats": 2, "label": None}
    assert (prov["worker_image"], prov["joern"], prov["endpoint"]) == ("rvh-worker:test", None, "http://llm:8000")
    assert prov["repos"]["demo"]["cpg"] == "off"
    assert (results / "summary.md").exists() and (results / "raw" / "demo" / "seed.json").exists()


def test_the_cpg_arm_is_recorded(tmp_path, offline):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN, "--repeats", "1", "--label", "cpg on"])
    (results,) = (root / "results").iterdir()
    prov = json.loads((results / "provenance.json").read_text())
    assert results.name.endswith("-quick-m-cpg-on")
    assert (prov["joern"], prov["arm"]["cpg"], prov["arm"]["repeats"]) == ("joern@test", True, 1)
    assert offline == [("demo", True)]


def test_resume_skips_finished_repos(tmp_path, offline):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN])
    (results,) = (root / "results").iterdir()
    eval_cli.main(["--corpus", str(root), "--resume", str(results), "--endpoint", "http://llm:8000", "--model", "m"])
    assert len(offline) == 1


def test_resume_refuses_another_model(tmp_path, offline):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN])
    (results,) = (root / "results").iterdir()
    with pytest.raises(SystemExit, match="resume it with that --model"):
        eval_cli.main(["--corpus", str(root), "--resume", str(results), "--endpoint", "http://llm:8000",
                       "--model", "other"])


def test_an_unknown_suite_is_refused(tmp_path, offline):
    with pytest.raises(SystemExit, match="--suite must be one of: quick"):
        eval_cli.main(["--corpus", str(build_corpus(tmp_path)), "--suite", "nope",
                       "--endpoint", "http://llm:8000", "--model", "m"])


def test_rescore_uses_the_current_labels(tmp_path, offline):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN])
    (results,) = (root / "results").iterdir()
    (root / "labels" / "demo.yaml").write_text(yaml.safe_dump([label(status="disputed")]))
    eval_cli.main(["rescore", str(results), "--corpus", str(root)])
    summary = json.loads((results / "summary.json").read_text())
    assert summary["labels"] == {}
    assert summary["metrics"]["findings"]["disputed"] == 2


def test_two_runs_of_the_same_arm_compare_as_no_difference(tmp_path, offline, capsys):
    root = build_corpus(tmp_path)
    eval_cli.main(["--corpus", str(root), *RUN])
    eval_cli.main(["--corpus", str(root), *RUN])
    a, b = sorted((root / "results").iterdir())
    capsys.readouterr()
    eval_cli.main(["compare", str(a), str(b)])
    out = capsys.readouterr().out
    assert "no significant difference" in out and "Smoke — not a decision" in out
