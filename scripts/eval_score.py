"""Score an eval: collect each task's findings, report status, and cost from
its run dir, match them against the corpus labels, and compute the metrics."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common import TASK_COMPLETED, TASK_FAILED, connect_db
from eval_corpus import Corpus, Label, Suite
from eval_match import LEVELS, Finding, Loc, distance, match_levels, normalize_path, parse_locations
from eval_seed import load_seed
from eval_trace import TraceStats, task_trace
from worker import _EXT_TO_LANG

# A task that ran to an end; pending/requeued ones (an interrupted eval) aren't scored.
FINISHED = (TASK_COMPLETED, TASK_FAILED)
# The shortest source line worth searching tool output for: `}` or `return x`
# appear everywhere and would prove nothing.
MIN_NEEDLE = 12


@dataclass
class TaskResult:
    task_id: int
    anchor: str
    repeat: int
    status: str
    reported: bool  # a worker_reports row exists
    findings: list[Finding]
    wall_seconds: float = 0.0
    attempts: int = 0
    trace: TraceStats = field(default_factory=TraceStats)

    @property
    def finished(self) -> bool:
        return self.status in FINISHED


def language_of(path: str) -> str:
    """The worker language module a file gets — the grouping prompts are tuned by."""
    return _EXT_TO_LANG.get(Path(path).suffix.lower(), "other")


def collect_repo(run_dir: Path, family_of) -> list[TaskResult]:
    """One TaskResult per seeded task in ``run_dir``."""
    seed = load_seed(run_dir)
    wall: dict[int, float] = defaultdict(float)
    attempts: Counter = Counter()
    journal = run_dir / "logs" / "batches.jsonl"
    if journal.exists():
        for line in journal.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                wall[record["task_id"]] += float(record.get("elapsed_seconds") or 0)
                attempts[record["task_id"]] += 1
    results = []
    with connect_db(run_dir) as conn:
        for key, info in seed["tasks"].items():
            task_id = int(key)
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
            reported = conn.execute("SELECT 1 FROM worker_reports WHERE task_id = ?", (task_id,)).fetchone()
            findings = [
                Finding(
                    task_id=task_id, title=row["title"], cwe=row["cwe"], family=family_of(row["cwe"]),
                    anchor_file=row["anchor_file"], summary=row["summary"],
                    locs=[loc for text in json.loads(row["related_files_json"] or "[]")
                          for loc in parse_locations(str(text), row["anchor_file"])],
                )
                for row in conn.execute("SELECT * FROM findings WHERE task_id = ?", (task_id,))
            ]
            results.append(TaskResult(
                task_id=task_id, anchor=info["anchor"], repeat=info["repeat"], status=status,
                reported=reported is not None, findings=findings, wall_seconds=wall[task_id],
                attempts=attempts[task_id], trace=task_trace(run_dir, task_id),
            ))
    return results


def sink_needle(label: Label, checkout: Path) -> str | None:
    """Source text to look for in tool output: the sink line, else the span's longest line."""
    try:
        lines = (checkout / label.file).read_text(errors="replace").splitlines()
    except OSError:
        return None

    def line(n: int) -> str:
        return lines[n - 1].strip() if 1 <= n <= len(lines) else ""

    text = line(label.sink_line)
    if len(text) < MIN_NEEDLE:
        text = max((line(n) for n in range(label.span[0], label.span[1] + 1)), key=len, default="")
    return text if len(text) >= MIN_NEEDLE else None


def classify(finding: Finding, repo_labels: list[Label]) -> str:
    """tp | known_fp | disputed | unmatched.

    The nearest label within slack decides: a positive makes the finding real;
    a negative makes it a known false positive only when the families match
    (a different claim at the same place isn't the disproven one, so the next
    nearest label decides). Labels are dense in reviewed files, so "nearest"
    matters: a finding on a disproven line shouldn't count as the positive a
    few lines away.
    """
    near = sorted((
        (dist, label.status == "disputed", label)
        for label in repo_labels
        if (dist := distance(label, finding)) is not None
    ), key=lambda item: item[:2])
    for _, disputed, label in near:
        if disputed:
            continue
        if label.truth == "positive":
            return "tp"
        if finding.family == label.family:
            return "known_fp"
    return "disputed" if any(disputed for _, disputed, _ in near) else "unmatched"


def recall_block(entries: list[dict], level: str, repeats: int) -> dict:
    """Recall at ``level``: mean per run, best single run, pooled (found in any run), all runs."""
    per_label = [[run[level] for run in e["runs"] if run is not None] for e in entries]
    per_label = [found for found in per_label if found]
    if not per_label:
        return {"n": 0, "mean": None, "best_single": None, "pooled": None, "all_runs": None}
    by_run = []
    for repeat in range(repeats):
        found = [e["runs"][repeat][level] for e in entries if e["runs"][repeat] is not None]
        if found:
            by_run.append(sum(found) / len(found))
    n = len(per_label)
    return {
        "n": n,
        "mean": sum(sum(found) / len(found) for found in per_label) / n,
        "best_single": max(by_run),
        "pooled": sum(any(found) for found in per_label) / n,
        "all_runs": sum(all(found) for found in per_label) / n,
    }


def grouped_recall(entries: list[dict], keys_of, repeats: int) -> dict:
    """Location-level recall per group; ``keys_of(entry)`` lists the entry's groups."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        for key in keys_of(entry):
            groups[key].append(entry)
    return {key: recall_block(group, "location", repeats) for key, group in sorted(groups.items())}


def cost_block(tasks: list[TaskResult]) -> dict:
    if not tasks:
        return {"tasks": 0}
    walls = sorted(task.wall_seconds for task in tasks)
    tools: Counter = Counter()
    unadvertised: Counter = Counter()
    for task in tasks:
        tools.update(task.trace.tool_calls)
        unadvertised.update(task.trace.unadvertised)
    parsed = [task for task in tasks if task.trace.parsed]
    return {
        "tasks": len(tasks),
        "attempts": sum(task.attempts for task in tasks),
        "wall_median_s": statistics.median(walls),
        "wall_p90_s": walls[math.ceil(0.9 * len(walls)) - 1],
        "wall_total_s": sum(walls),
        "traces_parsed": len(parsed),
        "turns_mean": statistics.mean(task.trace.turns for task in parsed) if parsed else None,
        "tool_calls": dict(sorted(tools.items())),
        "tool_errors": sum(task.trace.tool_errors for task in tasks),
        "unadvertised_tools": dict(unadvertised.most_common()),
        "input_tokens": sum(task.trace.input_tokens for task in tasks),
        "output_tokens": sum(task.trace.output_tokens for task in tasks),
    }


def miss_block(entries: list[dict]) -> dict:
    """For positive-label runs missed at location level: had the worker seen the sink line?"""
    names = {True: "looked_and_missed", False: "never_looked", None: "unknown"}
    counts: Counter = Counter()
    for entry in entries:
        for run in entry["runs"]:
            if run is not None and not run["location"]:
                counts[names[run["seen"]]] += 1
    return {name: counts[name] for name in ("never_looked", "looked_and_missed", "unknown")}


def score_eval(corpus: Corpus, suite: Suite, results: dict[str, list[TaskResult]], *,
               repeats: int, checkouts: dict[str, Path]) -> dict:
    """Match every finished task's findings to the suite's labels and compute the metrics."""
    anchors = {a.id: a for a in suite.anchors}
    by_run: dict[tuple[str, int], list[TaskResult]] = defaultdict(list)
    for tasks in results.values():
        for task in tasks:
            if task.finished:
                by_run[(task.anchor, task.repeat)].append(task)

    scope: dict[str, tuple[Label, list[str]]] = {}
    for anchor in suite.anchors:
        for label in corpus.labels_in_scope(anchor):
            if label.status != "disputed":
                scope.setdefault(label.id, (label, []))[1].append(anchor.id)

    positives: dict[str, dict] = {}
    negatives: dict[str, dict] = {}
    for label_id, (label, anchor_ids) in sorted(scope.items()):
        checkout = checkouts.get(label.repo)
        needle = sink_needle(label, checkout) if checkout else None
        runs: list[dict | None] = []
        for repeat in range(repeats):
            ran = [task for a in anchor_ids for task in by_run.get((a, repeat), [])]
            if not ran:
                runs.append(None)
                continue
            levels = match_levels(label, [f for task in ran for f in task.findings])
            if label.truth == "negative":
                runs.append({"hit": levels["location_cwe"]})
                continue
            traces = [task.trace for task in ran if task.trace.parsed]
            seen = None
            if not levels["location"] and needle and traces:
                seen = any(trace.saw(needle) for trace in traces)
            runs.append({**levels, "seen": seen})
        entry = {
            "repo": label.repo, "file": label.file, "sink_line": label.sink_line,
            "family": label.family, "language": language_of(label.file),
            "source": label.source, "status": label.status,
            "tags": sorted({tag for a in anchor_ids for tag in anchors[a].tags}),
            "anchors": anchor_ids, "runs": runs,
        }
        (negatives if label.truth == "negative" else positives)[label_id] = entry

    kinds: Counter = Counter()
    exhaustive: Counter = Counter()
    unmatched: dict[tuple, dict] = {}
    for repo_id, tasks in results.items():
        repo_labels = corpus.repo_labels(repo_id)
        for task in tasks:
            if not task.finished:
                continue
            for finding in task.findings:
                kind = classify(finding, repo_labels)
                kinds[kind] += 1
                if anchors[task.anchor].exhaustive and kind != "disputed":
                    exhaustive["findings"] += 1
                    exhaustive["tp"] += kind == "tp"
                if kind == "unmatched":
                    loc = finding.locs[0] if finding.locs else Loc(normalize_path(finding.anchor_file), 0, 0)
                    item = unmatched.setdefault((repo_id, loc.path, loc.start, finding.family), {
                        "repo": repo_id, "file": loc.path, "line": loc.start, "family": finding.family,
                        "cwe": finding.cwe, "title": finding.title, "summary": finding.summary[:2000],
                        "runs": set(), "task_ids": set(),
                    })
                    item["runs"].add(task.repeat)
                    item["task_ids"].add(task.task_id)

    finished = [task for tasks in results.values() for task in tasks if task.finished]
    entries = list(positives.values())
    metrics = {
        "recall": {level: recall_block(entries, level, repeats) for level in LEVELS},
        "by_language": grouped_recall(entries, lambda e: [e["language"]], repeats),
        "by_family": grouped_recall(entries, lambda e: [e["family"]], repeats),
        "by_source": grouped_recall(entries, lambda e: [e["source"]], repeats),
        "by_tag": grouped_recall(entries, lambda e: e["tags"], repeats),
        "findings": {"tp": kinds["tp"], "known_fp": kinds["known_fp"],
                     "unknown": kinds["unmatched"], "disputed": kinds["disputed"]},
        "negatives_hit": sum(any(run and run["hit"] for run in e["runs"]) for e in negatives.values()),
        "exhaustive": {
            "findings": exhaustive["findings"], "tp": exhaustive["tp"],
            "precision": exhaustive["tp"] / exhaustive["findings"] if exhaustive["findings"] else None,
        },
        "tasks": {"seeded": sum(len(tasks) for tasks in results.values()), "finished": len(finished)},
        "report_failure_rate": sum(not t.reported for t in finished) / len(finished) if finished else None,
        "cost": cost_block(finished),
        "miss_attribution": miss_block(entries),
    }
    return {
        "repeats": repeats,
        "labels": positives,
        "negatives": negatives,
        "metrics": metrics,
        "unmatched": [dict(item, runs=sorted(item["runs"]), task_ids=sorted(item["task_ids"]))
                      for item in unmatched.values()],
    }


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def render_summary_md(s: dict) -> str:
    """The human-readable summary of one eval."""
    p, m = s["provenance"], s["metrics"]
    lines = [f"# Eval {s['eval_id']}", ""]
    if s["suite_kind"] == "quick":
        lines += ["> **Smoke — not a decision.** Quick-suite numbers only show the machinery works.", ""]
    if not p.get("complete"):
        lines += ["> **Incomplete** — the eval was interrupted; unfinished runs are left out.", ""]
    arm = p["arm"]
    lines += [
        f"- Suite: `{s['suite']}` ({s['suite_kind']}), {s['repeats']} repeat(s)",
        f"- Model: `{p['model']}` @ {p['endpoint']}",
        f"- Arm: cpg={'on' if arm['cpg'] else 'off'}, parallelism={arm['parallelism']}"
        + (f", label={arm['label']}" if arm.get("label") else ""),
        # Results recorded before the rename carry vulnhunter_sha.
        f"- rvh {p.get('rvh_sha') or p.get('vulnhunter_sha')}, corpus {p['corpus_sha']}, image {p['worker_image']}",
    ]
    failed = sorted(repo for repo, info in p.get("repos", {}).items() if info.get("cpg") == "failed")
    if failed:
        lines.append(f"- **CPG failed** for {', '.join(failed)} — those repos ran without it")
    lines += ["", "## Recall", "",
              "| level | labels | mean per run | best single run | pooled | all runs |",
              "|---|---|---|---|---|---|"]
    for level in LEVELS:
        r = m["recall"][level]
        lines.append(f"| {level} | {r['n']} | {pct(r['mean'])} | {pct(r['best_single'])} "
                     f"| {pct(r['pooled'])} | {pct(r['all_runs'])} |")
    for title, key in (("By language", "by_language"), ("By family", "by_family"),
                       ("By label source", "by_source"), ("By tag", "by_tag")):
        lines += ["", f"## {title} (location level)", "",
                  "| group | labels | mean per run | pooled |", "|---|---|---|---|"]
        lines += [f"| {group} | {r['n']} | {pct(r['mean'])} | {pct(r['pooled'])} |" for group, r in m[key].items()]
    f, ex, tasks = m["findings"], m["exhaustive"], m["tasks"]
    lines += [
        "", "## Findings", "",
        f"- matched a positive label: {f['tp']}; known false positives: {f['known_fp']} "
        f"({m['negatives_hit']} negative label(s) hit); unknown, queued for adjudication: {f['unknown']}",
        f"- exhaustive anchors: {ex['tp']} of {ex['findings']} finding(s) real — precision {pct(ex['precision'])}",
        f"- report failures: {pct(m['report_failure_rate'])} of {tasks['finished']} finished task(s) "
        f"({tasks['seeded']} seeded)",
    ]
    c = m["cost"]
    if c.get("tasks"):
        turns = "—" if c["turns_mean"] is None else f"{c['turns_mean']:.1f}"
        unoffered = ", ".join(f"{name} ×{n}" for name, n in c["unadvertised_tools"].items()) or "none"
        lines += [
            "", "## Cost", "",
            f"- wall per task: median {c['wall_median_s']:.0f}s, p90 {c['wall_p90_s']:.0f}s; "
            f"total {c['wall_total_s'] / 3600:.1f}h over {c['attempts']} attempt(s)",
            f"- turns (mean): {turns}; tool calls: {c['tool_calls']}; tool errors: {c['tool_errors']}",
            f"- calls to tools the harness never offered: {unoffered}",
            f"- tokens: {c['input_tokens']:,} in / {c['output_tokens']:,} out",
        ]
    ma = m["miss_attribution"]
    lines += [
        "", "## Misses (location level)", "",
        f"- never looked at the sink line: {ma['never_looked']}; looked and missed: "
        f"{ma['looked_and_missed']}; unknown: {ma['unknown']}",
        "", "## Labels", "", "| label | family | runs (location) |", "|---|---|---|",
    ]
    for label_id, entry in s["labels"].items():
        marks = "".join("·" if run is None else ("✓" if run["location"] else "✗") for run in entry["runs"])
        lines.append(f"| {label_id} | {entry['family']} | {marks} |")
    return "\n".join(lines) + "\n"


def queue_unmatched(corpus_root: Path, summary: dict) -> int:
    """File each unmatched finding under adjudication/queue/ unless already decided; return how many are new.

    One file per (repo, file, line, family). A finding seen again (by this or
    another eval) adds or refreshes a sighting instead of a duplicate item.
    """
    queue = corpus_root / "adjudication" / "queue"
    decided = corpus_root / "adjudication" / "decided"
    queue.mkdir(parents=True, exist_ok=True)
    new = 0
    for item in summary["unmatched"]:
        key = f"{item['repo']}:{item['file']}:{item['line']}:{item['family']}"
        name = f"{item['repo']}-{hashlib.sha1(key.encode()).hexdigest()[:10]}.yaml"
        if (decided / name).exists():
            continue
        path = queue / name
        sighting = {"eval": summary["eval_id"], "runs": item["runs"], "task_ids": item["task_ids"]}
        if path.exists():
            entry = yaml.safe_load(path.read_text())
            entry["seen_in"] = [s for s in entry["seen_in"] if s["eval"] != summary["eval_id"]] + [sighting]
        else:
            entry = {"key": key, **{k: item[k] for k in ("repo", "file", "line", "family", "cwe", "title", "summary")},
                     "seen_in": [sighting], "verdict": None}
            new += 1
        path.write_text(yaml.safe_dump(entry, sort_keys=False, allow_unicode=True))
    return new
