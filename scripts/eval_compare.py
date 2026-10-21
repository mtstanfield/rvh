"""Paired comparison of two evals: per-label recall differences with bootstrap CIs.

Labels are the unit. Each label's per-run recall (the share of its runs that
found it) in eval A is paired with the same label's in eval B, and the mean
difference gets a 95% interval from resampling labels with replacement. A
difference counts only when that interval excludes zero.
"""
from __future__ import annotations

import random

from eval_score import pct

BOOTSTRAP_SAMPLES = 10_000
# Fewer labels than this can't support a verdict: resampling one or two labels
# gives a degenerate interval (a single flip reads as [+100%, +100%]).
MIN_LABELS = 5


def per_label_recall(summary: dict, level: str) -> dict[str, float]:
    """label id → share of its finished runs that found it at ``level``."""
    out = {}
    for label_id, entry in summary["labels"].items():
        found = [run[level] for run in entry["runs"] if run is not None]
        if found:
            out[label_id] = sum(found) / len(found)
    return out


def paired_bootstrap(diffs: list[float], *, samples: int = BOOTSTRAP_SAMPLES,
                     seed: int = 0) -> tuple[float, float, float]:
    """(mean difference, 2.5th percentile, 97.5th percentile) over label resamples."""
    n = len(diffs)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(samples))
    return sum(diffs) / n, means[int(0.025 * samples)], means[int(0.975 * samples) - 1]


def _block(ra: dict[str, float], rb: dict[str, float], ids: list[str]) -> dict:
    mean, lo, hi = paired_bootstrap([rb[i] - ra[i] for i in ids])
    return {
        "n": len(ids),
        "a": sum(ra[i] for i in ids) / len(ids),
        "b": sum(rb[i] for i in ids) / len(ids),
        "diff": mean,
        "ci95": [lo, hi],
        "significant": len(ids) >= MIN_LABELS and (lo > 0 or hi < 0),
    }


def compare(a: dict, b: dict, *, level: str = "location") -> dict:
    """Pair two eval summaries' labels; overall and per-family differences (b − a)."""
    ra, rb = per_label_recall(a, level), per_label_recall(b, level)
    shared = sorted(ra.keys() & rb.keys())
    families = sorted({a["labels"][i]["family"] for i in shared})
    return {
        "a": a["eval_id"], "b": b["eval_id"], "level": level,
        "smoke": "quick" in (a["suite_kind"], b["suite_kind"]),
        "same_suite": (a["suite"], a["suite_sha256"]) == (b["suite"], b["suite_sha256"]),
        "overall": _block(ra, rb, shared) if shared else None,
        "by_family": {fam: _block(ra, rb, [i for i in shared if a["labels"][i]["family"] == fam])
                      for fam in families},
        "gained": [i for i in shared if ra[i] == 0 < rb[i]],
        "lost": [i for i in shared if rb[i] == 0 < ra[i]],
        "only_a": sorted(ra.keys() - rb.keys()),
        "only_b": sorted(rb.keys() - ra.keys()),
        "cost": {"a": a["metrics"]["cost"], "b": b["metrics"]["cost"]},
    }


def render_compare_md(c: dict) -> str:
    lines = [f"# Compare {c['a']} → {c['b']} ({c['level']} level)", ""]
    if c["smoke"]:
        lines += ["> **Smoke — not a decision.** At least one side is a quick-suite eval.", ""]
    if not c["same_suite"]:
        lines += ["> **Different suites** — only labels both evals scored are compared.", ""]
    o = c["overall"]
    if o is None:
        return "\n".join(lines + ["No labels in common."]) + "\n"
    verdict = "**significant**" if o["significant"] else "no significant difference"
    lines += [
        f"Per-run recall over {o['n']} shared label(s): {pct(o['a'])} → {pct(o['b'])} "
        f"(diff {o['diff']:+.1%}, 95% CI [{o['ci95'][0]:+.1%}, {o['ci95'][1]:+.1%}]) — {verdict}.",
        "", "| family | labels | a | b | diff | 95% CI | significant |", "|---|---|---|---|---|---|---|",
    ]
    for fam, r in c["by_family"].items():
        lines.append(f"| {fam} | {r['n']} | {pct(r['a'])} | {pct(r['b'])} | {r['diff']:+.1%} "
                     f"| [{r['ci95'][0]:+.1%}, {r['ci95'][1]:+.1%}] | "
                     f"{'yes' if r['significant'] else ('too few labels' if r['n'] < MIN_LABELS else '')} |")
    if c["gained"]:
        lines += ["", f"Found only by b: {', '.join(c['gained'])}"]
    if c["lost"]:
        lines += ["", f"Found only by a: {', '.join(c['lost'])}"]
    ca, cb = c["cost"]["a"], c["cost"]["b"]
    if ca.get("tasks") and cb.get("tasks"):
        lines += ["", "| cost | a | b |", "|---|---|---|",
                  f"| median wall per task | {ca['wall_median_s']:.0f}s | {cb['wall_median_s']:.0f}s |",
                  f"| input tokens | {ca['input_tokens']:,} | {cb['input_tokens']:,} |",
                  f"| output tokens | {ca['output_tokens']:,} | {cb['output_tokens']:,} |",
                  f"| calls to unoffered tools | {sum(ca['unadvertised_tools'].values())} "
                  f"| {sum(cb['unadvertised_tools'].values())} |"]
    return "\n".join(lines) + "\n"
