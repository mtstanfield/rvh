#!/usr/bin/env python3
"""rvh eval — measure worker quality on a labeled corpus.

    ./rvh eval --corpus DIR --suite ID --endpoint URL --model NAME [options]
    ./rvh eval rescore RESULTS_DIR --corpus DIR
    ./rvh eval compare RESULTS_A RESULTS_B
    ./rvh eval validate --corpus DIR [--public] [--no-code]
    ./rvh eval pin SOURCE

A run checks out each suite repo at its pinned revision, seeds one task per
anchor × repeat in place of inventory/analyze/score, and dispatches them
through the sandbox, CPG sidecar, gateway, and engine a scan uses. Results
go to <corpus>/results/<eval-id>/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import llm  # noqa: E402
import sandbox  # noqa: E402
from common import TASK_COMPLETED, TASK_FAILED, connect_db, start_caffeinate, utc_now  # noqa: E402
from cpg_build import detect_primary_language, get_joern_version  # noqa: E402
from eval_checkout import ensure_checkout, pin  # noqa: E402
from eval_compare import compare, render_compare_md  # noqa: E402
from eval_corpus import (  # noqa: E402
    Anchor, Corpus, CorpusError, Repo, Suite, check_code, check_public, check_signoff, load_corpus,
)
from eval_score import collect_repo, queue_unmatched, render_summary_md, score_eval  # noqa: E402
from eval_seed import SEED_FILE, load_seed, seed_run  # noqa: E402
from image import ImageBuildError, ensure_worker_image  # noqa: E402
from preflight import PreflightError, run_preflight  # noqa: E402
from run_repo_vuln_hunt import build_engine_command, phase_cpg_build_and_sidecar, phase_cpg_teardown  # noqa: E402
from worker import log_progress  # noqa: E402

SUBCOMMANDS = ("run", "rescore", "compare", "validate", "pin")
PROVENANCE = "provenance.json"


def normalize_argv(argv: list[str]) -> list[str]:
    """``rvh eval --corpus …`` means ``rvh eval run --corpus …``."""
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return ["run", *argv]
    return argv


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-.") or "x"


def make_eval_id(suite_id: str, model: str, label: str | None, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    return "-".join([stamp, _slug(suite_id), _slug(model), *([_slug(label)] if label else [])])


def unique_dir(parent: Path, name: str) -> Path:
    """``parent/name``, or ``parent/name-2``, ``-3``, … if taken."""
    path, n = parent / name, 2
    while path.exists():
        path, n = parent / f"{name}-{n}", n + 1
    return path


def load_or_exit(path: Path) -> Corpus:
    try:
        return load_corpus(path)
    except CorpusError as exc:
        raise SystemExit(f"[corpus] {exc}")


def read_summary(path: Path) -> dict:
    path = path.expanduser()
    return json.loads((path / "summary.json" if path.is_dir() else path).read_text())


def git_rev(path: Path) -> str | None:
    """HEAD of the repo at ``path``, suffixed -dirty when tracked files have changes."""
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        return None
    dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    return head.stdout.strip() + ("-dirty" if dirty else "")


def new_provenance(corpus: Corpus, suite: Suite, *, eval_id: str, model: str, endpoint: str,
                   cpg: bool, parallelism: int, repeats: int, label: str | None) -> dict:
    """Everything needed to say what an eval measured; the suite's anchors are snapshotted
    so a later rescore scores the same scope even if the suite file changes."""
    return {
        "eval_id": eval_id, "started_at": utc_now(), "finished_at": None, "complete": False,
        "rvh_sha": git_rev(SCRIPT_DIR.parent), "corpus_sha": git_rev(corpus.root),
        "worker_image": None, "joern": get_joern_version() if cpg else None, "harness": "goose",
        "model": model, "endpoint": endpoint,
        "arm": {"cpg": cpg, "parallelism": max(1, parallelism), "repeats": max(1, repeats), "label": label},
        "suite": suite.id, "suite_kind": suite.kind, "suite_sha256": suite.sha256,
        "suite_anchors": [asdict(anchor) for anchor in suite.anchors],
        "repos": {},
    }


def suite_from_provenance(prov: dict) -> Suite:
    anchors = [Anchor(**{**a, "region": tuple(a["region"]) if a["region"] else None})
               for a in prov["suite_anchors"]]
    return Suite(id=prov["suite"], kind=prov["suite_kind"], repeats=prov["arm"]["repeats"],
                 frozen=True, anchors=anchors, sha256=prov["suite_sha256"])


def write_provenance(eval_dir: Path, prov: dict) -> None:
    (eval_dir / PROVENANCE).write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")


def unfinished_tasks(run_dir: Path) -> int:
    with connect_db(run_dir) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN (?, ?)",
                            (TASK_COMPLETED, TASK_FAILED)).fetchone()[0]


def dispatch_repo(*, repo: Repo, checkout: Path, run_dir: Path, image: str, endpoint: str, api_key: str,
                  model: str, parallelism: int, cpg: bool, cpg_dir: Path) -> dict:
    """Run a seeded repo's pending tasks through the real sandbox, CPG sidecar, gateway, and engine."""
    prefix = sandbox.run_prefix(checkout)
    leftovers = sandbox.reclaim(prefix)
    if leftovers:
        log_progress(f"[sandbox] removed {len(leftovers)} container(s) left by an earlier run")
    network = sandbox.create_network(prefix)
    cpg_status = "off"
    try:
        if cpg:
            cpg_dir.mkdir(parents=True, exist_ok=True)
            state = phase_cpg_build_and_sidecar(
                repo_path=checkout, run_dir=run_dir, lang=repo.cpg_lang or detect_primary_language(run_dir),
                prefix=prefix, network=network, cpg_dir=cpg_dir,
            )
            cpg_status = "ok" if state else "failed"
            # anchor_supported_by_cpg reads <run_dir>/cpg/metadata.json.
            if state and not (run_dir / "cpg").exists():
                (run_dir / "cpg").symlink_to(cpg_dir, target_is_directory=True)
        sandbox.start_gateway(prefix, image=image, endpoint=endpoint, api_key=api_key, network=network)
        engine_args = SimpleNamespace(repo=str(checkout), parallelism=parallelism, min_priority=1,
                                      max_tasks=None, debug=False, model=model)
        cmd, env = build_engine_command(engine_args, run_dir, image, network, cpg_status == "ok")
        returncode = subprocess.run(cmd, env=dict(os.environ, **env)).returncode
    finally:
        phase_cpg_teardown(run_dir)
        sandbox.reclaim(prefix)
    return {"cpg": cpg_status, "engine_exit": returncode}


def rescore(eval_dir: Path, corpus: Corpus) -> dict:
    """Score an eval's raw results against the corpus's current labels; write the summary and queue."""
    prov = json.loads((eval_dir / PROVENANCE).read_text())
    suite = suite_from_provenance(prov)
    results, checkouts = {}, {}
    for seed_file in sorted((eval_dir / "raw").glob(f"*/{SEED_FILE}")):
        seed = load_seed(seed_file.parent)
        repo = corpus.repos.get(seed["repo"])
        if repo is None or repo.rev != seed["rev"]:
            log_progress(f"[eval] skipping {seed['repo']}: re-pinned since this eval, so its labels may not line up")
            continue
        results[repo.id] = collect_repo(seed_file.parent, corpus.family_of)
        checkouts[repo.id] = ensure_checkout(repo, corpus.cache_dir)
    summary = score_eval(corpus, suite, results, repeats=prov["arm"]["repeats"], checkouts=checkouts)
    summary.update(eval_id=prov["eval_id"], suite=suite.id, suite_kind=suite.kind,
                   suite_sha256=suite.sha256, provenance=prov)
    (eval_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (eval_dir / "summary.md").write_text(render_summary_md(summary))
    queued = queue_unmatched(corpus.root, summary)
    log_progress(f"[eval] {eval_dir / 'summary.md'} — {queued} new finding(s) queued for adjudication")
    return summary


@contextmanager
def stop_on_sigint():
    """First Ctrl+C: stop after the current repo (its engine requeues in-flight tasks); second: exit now."""
    state = {"requested": False}

    def handler(signum, frame):
        if state["requested"]:
            raise SystemExit(1)
        state["requested"] = True
        log_progress("\n[eval] stopping after this repo — finish later with --resume (Ctrl+C again to force)")

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)


def cmd_run(args) -> None:
    start_caffeinate()
    corpus = load_or_exit(args.corpus)
    args.endpoint = llm.normalize_endpoint(args.endpoint)
    args.api_key = args.api_key or os.environ.get("RVH_API_KEY") or "dummy"
    os.environ["RVH_API_KEY"] = args.api_key  # child stages read it from here, never argv

    if args.resume:
        eval_dir = args.resume.expanduser().resolve()
        prov = json.loads((eval_dir / PROVENANCE).read_text())
        if prov["model"] != args.model:
            raise SystemExit(f"[eval] {eval_dir.name} ran {prov['model']!r}; resume it with that --model")
    else:
        suite = corpus.suites.get(args.suite or "")
        if suite is None:
            raise SystemExit(f"[eval] --suite must be one of: {', '.join(sorted(corpus.suites)) or '(no suites)'}")
        eval_dir = unique_dir(corpus.root / "results", make_eval_id(suite.id, args.model, args.label))
        prov = new_provenance(corpus, suite, eval_id=eval_dir.name, model=args.model, endpoint=args.endpoint,
                              cpg=not args.no_cpg, parallelism=args.parallelism,
                              repeats=args.repeats or suite.repeats, label=args.label)
    suite = suite_from_provenance(prov)

    try:
        run_preflight(args.endpoint, args.model, args.api_key, warn=log_progress)
        image = ensure_worker_image(log_progress)
    except (PreflightError, ImageBuildError) as exc:
        raise SystemExit(f"[preflight] {exc}")
    if prov["worker_image"] not in (None, image):
        raise SystemExit(f"[eval] {eval_dir.name} ran on {prov['worker_image']} but this rvh builds "
                         f"{image}; start a new eval")
    prov["worker_image"] = image
    eval_dir.mkdir(parents=True, exist_ok=True)
    write_provenance(eval_dir, prov)

    arm = prov["arm"]
    log_progress(f"[eval] {eval_dir.name}: {len(suite.anchors)} anchor(s) × {arm['repeats']} repeat(s)")
    with stop_on_sigint() as stop:
        for repo_id, anchors in suite.by_repo().items():
            if stop["requested"]:
                break
            repo = corpus.repos[repo_id]
            run_dir = eval_dir / "raw" / repo_id
            if (run_dir / SEED_FILE).exists() and load_seed(run_dir)["rev"] != repo.rev:
                raise SystemExit(f"[eval] {repo_id} was re-pinned since this eval started; start a new eval")
            checkout = ensure_checkout(repo, corpus.cache_dir)
            if not (run_dir / "state.db").exists():
                seed_run(run_dir, repo=repo, checkout=checkout, anchors=anchors, repeats=arm["repeats"])
            if not unfinished_tasks(run_dir):
                continue
            log_progress(f"[eval] {repo_id}@{repo.rev[:12]}: {len(anchors)} anchor(s)")
            try:
                outcome = dispatch_repo(
                    repo=repo, checkout=checkout, run_dir=run_dir, image=image,
                    endpoint=args.endpoint, api_key=args.api_key, model=args.model,
                    parallelism=arm["parallelism"], cpg=arm["cpg"],
                    cpg_dir=corpus.cache_dir / "cpg" / f"{repo.id}@{repo.rev[:12]}",
                )
            except sandbox.SandboxError as exc:
                raise SystemExit(f"[sandbox] {exc}")
            prov["repos"][repo_id] = {"rev": repo.rev, **outcome}
            write_provenance(eval_dir, prov)
    prov["complete"] = all(
        (eval_dir / "raw" / repo_id / "state.db").exists() and not unfinished_tasks(eval_dir / "raw" / repo_id)
        for repo_id in suite.by_repo()
    )
    prov["finished_at"] = utc_now()
    write_provenance(eval_dir, prov)
    rescore(eval_dir, corpus)


def cmd_rescore(args) -> None:
    rescore(args.results.expanduser().resolve(), load_or_exit(args.corpus))


def cmd_validate(args) -> None:
    corpus = load_or_exit(args.corpus)
    errors = check_signoff(corpus) + (check_public(corpus) if args.public else [])
    if not args.no_code:
        errors += check_code(corpus, lambda repo: ensure_checkout(repo, corpus.cache_dir))
    for error in errors:
        print(f"  - {error}")
    if errors:
        raise SystemExit(f"[validate] {len(errors)} problem(s)")
    positives = sum(label.truth == "positive" for label in corpus.labels.values())
    print(f"[validate] ok: {len(corpus.repos)} repo(s), {positives} positive and "
          f"{len(corpus.labels) - positives} negative label(s), {len(corpus.suites)} suite(s)")


def cmd_pin(args) -> None:
    pinned = pin(args.source)
    warning = pinned.pop("warning", None)
    entry = {"id": "CHANGE-ME", "source": args.source, **pinned, "framework_hints": []}
    print(yaml.safe_dump([entry], sort_keys=False).rstrip())
    if warning:
        print(f"# warning: {warning}", file=sys.stderr)


def cmd_compare(args) -> None:
    markdown = render_compare_md(compare(read_summary(args.a), read_summary(args.b), level=args.level))
    print(markdown, end="")
    if args.out:
        args.out.write_text(markdown)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rvh eval", description="Measure worker quality on a labeled corpus.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a suite (the default subcommand)")
    run.add_argument("--corpus", required=True, type=Path, help="Corpus directory")
    run.add_argument("--suite", help="Suite id (suites/<id>.yaml); not needed with --resume")
    run.add_argument("--endpoint", required=True, help="OpenAI-compatible endpoint URL")
    run.add_argument("--model", required=True, help="Model name")
    run.add_argument("--api-key", default=None, help="API key (default: $RVH_API_KEY, else 'dummy')")
    run.add_argument("--repeats", type=int, default=None, help="Runs per anchor (default: the suite's)")
    run.add_argument("--parallelism", type=int, default=1, help="Concurrent workers per repo (default 1)")
    run.add_argument("--no-cpg", action="store_true", help="Run without the joern CPG (an ablation arm)")
    run.add_argument("--label", default=None, help="Name for this arm; becomes part of the eval id")
    run.add_argument("--resume", type=Path, default=None, metavar="RESULTS_DIR",
                     help="Finish an interrupted eval")
    run.set_defaults(func=cmd_run)

    rs = sub.add_parser("rescore", help="Re-score an eval against the corpus's current labels")
    rs.add_argument("results", type=Path, help="Results dir of the eval")
    rs.add_argument("--corpus", required=True, type=Path)
    rs.set_defaults(func=cmd_rescore)

    cmp = sub.add_parser("compare", help="Paired comparison of two evals")
    cmp.add_argument("a", type=Path, help="Results dir (or summary.json) of the baseline")
    cmp.add_argument("b", type=Path, help="Results dir (or summary.json) of the candidate")
    cmp.add_argument("--level", choices=("file", "location", "location_cwe"), default="location")
    cmp.add_argument("--out", type=Path, default=None, help="Also write the report here")
    cmp.set_defaults(func=cmd_compare)

    val = sub.add_parser("validate", help="Check a corpus")
    val.add_argument("--corpus", required=True, type=Path)
    val.add_argument("--public", action="store_true", help="Apply the export guard (for evals/public/)")
    val.add_argument("--no-code", action="store_true", help="Skip checking labels against the pinned code")
    val.set_defaults(func=cmd_validate)

    pn = sub.add_parser("pin", help="Print a repos.yaml entry pinning a URL or local path")
    pn.add_argument("source")
    pn.set_defaults(func=cmd_pin)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    args.func(args)


if __name__ == "__main__":
    main()
