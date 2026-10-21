"""Collecting and scoring an eval (scripts/eval_score.py)."""
import json

import pytest

from common import connect_db
from eval_corpus import Label, load_corpus
from eval_score import collect_repo, score_eval, sink_needle
from eval_seed import seed_run
from tests.evalkit import APP_PY, anchor, label, write_corpus

SHA = "c" * 40
# Line 20 is the command-injection sink; line 5 is where a disproven XSS claim pointed.
TOOL_PY = ("".join(f"x{i} = {i}\n" for i in range(1, 20))
           + "subprocess.run(cmd, shell=True)  # sink\n"
           + "".join(f"y{i} = {i}\n" for i in range(21, 31)))
UNOFFERED = {"type": "toolResponse",
             "toolResult": {"status": "error", "error": "-32600: Tool 'read_file' was not advertised"}}


def _report(run_dir, task_id, anchor_file, findings):
    with connect_db(run_dir) as conn:
        report_id = conn.execute(
            "INSERT INTO worker_reports (task_id, report_path, title, verdict, severity, confidence,"
            " metadata_json, created_at) VALUES (?, 'r.md', 't', 'finding', 'high', 'high', '{}', 'now')",
            (task_id,),
        ).lastrowid
        for where, cwe in findings:
            conn.execute(
                "INSERT INTO findings (task_id, report_id, title, finding_type, severity, confidence, status,"
                " anchor_file, related_files_json, summary, cwe, created_at)"
                " VALUES (?, ?, ?, 'x', 'high', 'high', 'candidate', ?, ?, 'summary', ?, 'now')",
                (task_id, report_id, f"finding at {where}", anchor_file, json.dumps([where]), cwe),
            )
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))


def _fail(run_dir, task_id):
    with connect_db(run_dir) as conn:
        conn.execute("UPDATE tasks SET status = 'failed' WHERE id = ?", (task_id,))


def _journal(run_dir, records):
    with (run_dir / "logs" / "batches.jsonl").open("a") as fh:
        for task_id, seconds in records:
            fh.write(json.dumps({"task_id": task_id, "elapsed_seconds": seconds}) + "\n")


def _trace(run_dir, task_id, tool_output, *extra_responses):
    task_dir = run_dir / "tasks" / f"auto-0001-abcd-task-{task_id}"
    task_dir.mkdir(parents=True)
    session = {"messages": [
        {"role": "assistant", "content": [
            {"type": "toolRequest", "toolCall": {"status": "success", "value": {"name": "shell", "arguments": {}}}}]},
        {"role": "user", "content": [
            {"type": "toolResponse",
             "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": tool_output}]}}},
            *extra_responses]},
    ], "metadata": {"input_tokens": 1000, "output_tokens": 10}}
    (task_dir / "stdout.txt").write_text("banner\n" + json.dumps(session))


@pytest.fixture
def scored(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "app.py").write_text(APP_PY)
    (checkout / "tool.py").write_text(TOOL_PY)
    corpus = load_corpus(write_corpus(
        tmp_path / "corpus",
        repos=[{"id": "demo", "source": "https://example.com/demo.git", "commit": SHA}],
        labels=[
            label(),  # demo-eval-1: app.py:3, CWE-95
            label(id="demo-cmd-1", file="tool.py", sink_line=20, span=[20, 20],
                  cwe="CWE-78", family="command-injection"),
            label(id="demo-fp-1", file="tool.py", sink_line=5, span=[5, 5],
                  cwe="CWE-79", family="xss", truth="negative"),
        ],
        suites={"quick": {"kind": "quick", "repeats": 2, "anchors": [
            anchor(id="a-app", exhaustive=True, tags=["python", "web"]),
            anchor(id="a-tool", path="tool.py", tags=["python"]),
        ]}},
    ))
    suite = corpus.suites["quick"]
    run_dir = tmp_path / "raw" / "demo"
    seed = seed_run(run_dir, repo=corpus.repos["demo"], checkout=checkout, anchors=suite.anchors, repeats=2)
    ids = {(t["anchor"], t["repeat"]): int(k) for k, t in seed["tasks"].items()}
    _report(run_dir, ids[("a-app", 0)], "app.py", [("app.py:3", "CWE-94")])
    _report(run_dir, ids[("a-tool", 0)], "tool.py", [("tool.py:5", "CWE-79")])
    _report(run_dir, ids[("a-app", 1)], "app.py", [("/workspace/app.py:4", "CWE-78"), ("helpers.py:12", "CWE-918")])
    _fail(run_dir, ids[("a-tool", 1)])
    _journal(run_dir, [(ids[("a-app", 0)], 100), (ids[("a-tool", 0)], 200), (ids[("a-app", 1)], 300),
                       (ids[("a-tool", 1)], 400), (ids[("a-tool", 1)], 500)])
    _trace(run_dir, ids[("a-tool", 0)], "   20\tsubprocess.run(cmd, shell=True)  # sink")
    _trace(run_dir, ids[("a-tool", 1)], "    1\tx1 = 1", UNOFFERED)
    summary = score_eval(corpus, suite, {"demo": collect_repo(run_dir, corpus.family_of)},
                         repeats=2, checkouts={"demo": checkout})
    summary.update(
        eval_id="20260924-1200-quick-m", suite="quick", suite_kind="quick", suite_sha256=suite.sha256,
        provenance={"model": "m", "endpoint": "http://e", "complete": True, "rvh_sha": "v",
                    "corpus_sha": "c", "worker_image": "img", "repos": {},
                    "arm": {"cpg": True, "parallelism": 1, "repeats": 2, "label": None}},
    )
    return corpus, summary, ids


def test_recall_at_each_level(scored):
    _, s, _ = scored
    r = s["metrics"]["recall"]
    # location: eval-1 found in both runs, cmd-1 in neither (tool.py:5 is 15 lines off).
    assert r["location"] == {"n": 2, "mean": 0.5, "best_single": 0.5, "pooled": 0.5, "all_runs": 0.5}
    # location_cwe: run 1 called eval-1 CWE-78, the wrong family.
    assert r["location_cwe"] == {"n": 2, "mean": 0.25, "best_single": 0.5, "pooled": 0.5, "all_runs": 0.0}
    # file: the tool.py:5 finding puts run 0 in cmd-1's file.
    assert r["file"] == {"n": 2, "mean": 0.75, "best_single": 1.0, "pooled": 1.0, "all_runs": 0.5}


def test_findings_are_classified(scored):
    _, s, _ = scored
    m = s["metrics"]
    assert m["findings"] == {"tp": 2, "known_fp": 1, "unknown": 1, "disputed": 0}
    assert m["negatives_hit"] == 1
    # a-app is exhaustive: 3 findings there, 2 real.
    assert m["exhaustive"] == {"findings": 3, "tp": 2, "precision": pytest.approx(2 / 3)}


def test_report_failures_and_cost(scored):
    _, s, _ = scored
    m = s["metrics"]
    assert m["tasks"] == {"seeded": 4, "finished": 4}
    assert m["report_failure_rate"] == 0.25
    c = m["cost"]
    # walls 100, 200, 300, 400+500 → median 250, p90 900.
    assert (c["tasks"], c["attempts"], c["wall_median_s"], c["wall_p90_s"], c["wall_total_s"]) == \
        (4, 5, 250.0, 900.0, 1500.0)
    assert (c["traces_parsed"], c["tool_calls"], c["input_tokens"], c["turns_mean"]) == (2, {"shell": 2}, 2000, 1.0)
    assert c["unadvertised_tools"] == {"read_file": 1}


def test_misses_are_attributed_from_the_trace(scored):
    _, s, _ = scored
    assert [run["seen"] for run in s["labels"]["demo-cmd-1"]["runs"]] == [True, False]
    assert s["metrics"]["miss_attribution"] == {"never_looked": 1, "looked_and_missed": 1, "unknown": 0}


def test_breakdowns(scored):
    _, s, _ = scored
    m = s["metrics"]
    assert m["by_family"]["code-injection"]["mean"] == 1.0
    assert m["by_family"]["command-injection"]["mean"] == 0.0
    assert (m["by_tag"]["web"]["n"], m["by_tag"]["python"]["n"]) == (1, 2)
    assert m["by_language"] == {"python": m["recall"]["location"]}
    assert m["by_source"]["harness"]["n"] == 2


def test_unmatched_findings_are_collected_once_per_location(scored):
    _, s, ids = scored
    (item,) = s["unmatched"]
    assert (item["file"], item["line"], item["family"], item["runs"]) == ("helpers.py", 12, "CWE-918", [1])
    assert item["task_ids"] == [ids[("a-app", 1)]]


def test_unfinished_runs_are_left_out(scored, tmp_path):
    corpus, _, ids = scored
    run_dir = tmp_path / "raw" / "demo"
    with connect_db(run_dir) as conn:
        conn.execute("UPDATE tasks SET status = 'pending' WHERE id = ?", (ids[("a-tool", 1)],))
    s = score_eval(corpus, corpus.suites["quick"], {"demo": collect_repo(run_dir, corpus.family_of)},
                   repeats=2, checkouts={})
    assert s["labels"]["demo-cmd-1"]["runs"][1] is None
    assert s["metrics"]["tasks"] == {"seeded": 4, "finished": 3}


def test_sink_needle_falls_back_to_the_longest_line_in_the_span(tmp_path):
    (tmp_path / "f.py").write_text("def handler(request):\n    }\n    return do_the_dangerous_thing(request)\n")
    raw = label(file="f.py", sink_line=2, span=[1, 3])
    assert sink_needle(Label(**{**raw, "span": (1, 3)}), tmp_path) == "return do_the_dangerous_thing(request)"
    (tmp_path / "g.py").write_text("}\n)\n")
    raw = label(file="g.py", sink_line=1, span=[1, 2])
    assert sink_needle(Label(**{**raw, "span": (1, 2)}), tmp_path) is None


import yaml  # noqa: E402

from eval_score import queue_unmatched, render_summary_md  # noqa: E402


def test_summary_markdown(scored):
    _, s, _ = scored
    md = render_summary_md(s)
    assert "Smoke — not a decision" in md
    assert "| location | 2 | 50% | 50% | 50% | 50% |" in md
    assert "| demo-cmd-1 | command-injection | ✗✗ |" in md
    assert "never looked at the sink line: 1" in md
    assert "calls to tools the harness never offered: read_file ×1" in md
    assert "- rvh v, corpus c, image img" in md


def test_summary_markdown_reads_pre_rename_provenance(scored):
    """Results recorded before the rename carry vulnhunter_sha."""
    _, s, _ = scored
    prov = dict(s["provenance"])
    prov["vulnhunter_sha"] = prov.pop("rvh_sha")
    assert "- rvh v, corpus c, image img" in render_summary_md({**s, "provenance": prov})


def test_the_queue_adds_new_items_and_records_sightings(scored):
    corpus, s, _ = scored
    assert queue_unmatched(corpus.root, s) == 1
    assert queue_unmatched(corpus.root, s) == 0  # the same eval again: no duplicate
    (path,) = (corpus.root / "adjudication" / "queue").glob("*.yaml")
    item = yaml.safe_load(path.read_text())
    assert (item["file"], item["line"], item["verdict"]) == ("helpers.py", 12, None)
    assert [x["eval"] for x in item["seen_in"]] == [s["eval_id"]]
    assert queue_unmatched(corpus.root, {**s, "eval_id": "another"}) == 0
    assert [x["eval"] for x in yaml.safe_load(path.read_text())["seen_in"]] == [s["eval_id"], "another"]


def test_decided_items_are_not_requeued(scored):
    corpus, s, _ = scored
    queue_unmatched(corpus.root, s)
    (path,) = (corpus.root / "adjudication" / "queue").glob("*.yaml")
    decided = corpus.root / "adjudication" / "decided"
    decided.mkdir()
    path.rename(decided / path.name)
    assert queue_unmatched(corpus.root, s) == 0
    assert not list((corpus.root / "adjudication" / "queue").glob("*.yaml"))


from eval_match import Finding, Loc  # noqa: E402
from eval_score import classify  # noqa: E402


def _lab(**overrides) -> Label:
    raw = label(**overrides)
    return Label(**{**raw, "span": tuple(raw["span"])})


def _at(line: int, family: str) -> Finding:
    return Finding(task_id=1, title="t", cwe=None, family=family, anchor_file="app.py", locs=[Loc("app.py", line, line)])


def test_classify_picks_the_nearest_label_when_several_are_in_slack():
    """Dense labels: a finding next to a disproven claim is that claim, not the positive 8 lines away."""
    labels = [_lab(id="pos", sink_line=10, span=[10, 10]),
              _lab(id="neg", sink_line=18, span=[18, 18], cwe="CWE-79", family="xss", truth="negative")]
    assert classify(_at(18, "xss"), labels) == "known_fp"
    assert classify(_at(11, "xss"), labels) == "tp"
    # A different claim at the negative's line isn't the disproven claim: the positive in slack still counts.
    assert classify(_at(18, "code-injection"), labels) == "tp"
