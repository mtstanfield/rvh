#!/usr/bin/env python3
"""Decide and create per-region sub-tasks for large anchor files.

A single worker pass samples a file's vulnerabilities with a positional bias —
the tail of a long file starves under attention budget. This module
splits qualifying files into contiguous, function-boundary-aligned slices, one
sub-task each, so every region gets a fresh budget.

Pure helpers (`should_decompose`, `plan_slices`) are unit-tested without a DB
or CPG; `expand_run` is the DB-mutating driver the orchestrator calls after the
CPG is ready and before dispatch.
"""
from __future__ import annotations

import os
from pathlib import Path

from cpg_client import CpgClient  # noqa: E402  (monkeypatched in tests)
import cpg_queries  # noqa: E402

DECOMP_MIN_TOKENS = int(os.environ.get("RVH_DECOMP_MIN_TOKENS", "15000"))
DECOMP_SLICE_BUDGET = int(os.environ.get("RVH_DECOMP_SLICE_BUDGET", "12000"))
DECOMP_MAX_SLICES = int(os.environ.get("RVH_DECOMP_MAX_SLICES", "8"))
DECOMP_MIN_PRIORITY = int(os.environ.get("RVH_DECOMP_MIN_PRIORITY", "3"))
# A slice smaller than this fraction of the budget is a boundary artifact (e.g.
# a 4-line tail) and gets merged into a neighbor rather than burning a pass.
DECOMP_MIN_SLICE_FRACTION = float(os.environ.get("RVH_DECOMP_MIN_SLICE_FRACTION", "0.15"))


def plan_slices(
    total_lines: int,
    fn_start_lines,
    file_tokens: int,
    *,
    budget_tokens: int = DECOMP_SLICE_BUDGET,
    max_slices: int = DECOMP_MAX_SLICES,
) -> list[tuple[int, int]]:
    """Split [1, total_lines] into contiguous slices cut at function starts.

    Greedily packs whole functions into a slice until the next would exceed
    ``budget_tokens`` (tokens apportioned by line share). A single function
    bigger than the budget becomes its own (oversized) slice — never a
    mid-function cut. Enforces ``max_slices`` by merging the narrowest
    adjacent pair. Returns 1-based inclusive ranges that tile the file.
    """
    if total_lines <= 0:
        return []
    cuts = sorted({int(l) for l in fn_start_lines if 1 < int(l) <= total_lines})
    if not cuts:
        return [(1, total_lines)]

    starts = [1] + cuts
    segs: list[tuple[int, int]] = []
    for i, s in enumerate(starts):
        e = (starts[i + 1] - 1) if i + 1 < len(starts) else total_lines
        if e >= s:
            segs.append((s, e))

    tok_per_line = max(file_tokens / total_lines, 1e-9)

    def seg_tokens(seg: tuple[int, int]) -> float:
        return (seg[1] - seg[0] + 1) * tok_per_line

    slices: list[tuple[int, int]] = []
    cur_start, cur_end = segs[0]
    cur_tok = seg_tokens(segs[0])
    for seg in segs[1:]:
        st = seg_tokens(seg)
        if cur_tok + st > budget_tokens and cur_tok > 0:
            slices.append((cur_start, cur_end))
            cur_start, cur_end, cur_tok = seg[0], seg[1], st
        else:
            cur_end = seg[1]
            cur_tok += st
    slices.append((cur_start, cur_end))

    # Absorb degenerate (sub-floor) slices into a neighbor — a function-boundary
    # artifact must not leave a tiny trailing slice burning a whole pass.
    min_tokens = budget_tokens * DECOMP_MIN_SLICE_FRACTION
    i = 0
    while len(slices) > 1 and i < len(slices):
        s, e = slices[i]
        if (e - s + 1) * tok_per_line < min_tokens:
            if i == 0:
                slices[1] = (slices[0][0], slices[1][1])
                del slices[0]  # re-check new slices[0]; don't advance
            else:
                slices[i - 1] = (slices[i - 1][0], slices[i][1])
                del slices[i]
                i -= 1
        else:
            i += 1

    # Hard cap: merge the narrowest adjacent pair until within max_slices.
    while len(slices) > max_slices:
        i = min(range(len(slices) - 1), key=lambda k: slices[k + 1][1] - slices[k][0])
        slices[i] = (slices[i][0], slices[i + 1][1])
        del slices[i + 1]
    return slices


def should_decompose(
    *, file_tokens: int, fn_count: int, priority: int, cpg_available: bool
) -> bool:
    """True when a file is worth splitting: CPG present, big enough, has
    multiple functions, and high enough priority to spend the extra passes."""
    return (
        cpg_available
        and file_tokens > DECOMP_MIN_TOKENS
        and fn_count >= 2
        and priority >= DECOMP_MIN_PRIORITY
    )


def decorator_aware_starts(lines, def_lines) -> list[int]:
    """Move each function start (a ``def``/``class`` line) up past its
    contiguous ``@decorator`` lines, so a slice boundary keeps a function's
    decorators with it instead of orphaning them into the previous slice —
    which produced false 'missing authorization' findings when an
    ``@login_required`` landed one line above a slice boundary.

    ``lines`` is the file split on newlines (0-indexed); ``def_lines`` are
    1-based ``def`` line numbers. Returns deduped, sorted 1-based starts.
    """
    out: list[int] = []
    for raw in def_lines:
        start = int(raw)
        while start > 1 and (start - 2) < len(lines):
            if lines[start - 2].strip().startswith("@"):
                start -= 1
            else:
                break
        out.append(start)
    return sorted(set(out))


def _method_lines(client, path: str) -> list[int]:
    """Function/method start lines in ``path`` via the CPG (deduped, sorted)."""
    rows = client.query(cpg_queries.build_entrypoints(file=path, limit=1000))
    out: list[int] = []
    for r in rows or []:
        if path in str(r.get("file", "")) and r.get("line"):
            out.append(int(r["line"]))
    return sorted(set(out))


def expand_run(run_dir, cpg_endpoint: str) -> dict:
    """Expand qualifying pending file-tasks into per-region sub-tasks.

    Runs after CPG is ready, before dispatch. Best-effort and idempotent: a
    file is only expanded while its task is ``pending`` with a NULL region;
    expanded files have their task flipped to ``decomposed`` (non-dispatchable
    audit record) so a re-run skips them. Returns {"files": n, "slices": m}.
    """
    from common import connect_db, utc_now  # noqa: PLC0415
    from common import count_tokens  # noqa: PLC0415

    counts = {"files": 0, "slices": 0}
    if not cpg_endpoint:
        return counts
    repo_root = Path(run_dir).resolve().parent
    client = CpgClient(cpg_endpoint)

    with connect_db(run_dir) as conn:
        rows = conn.execute(
            "SELECT t.id AS task_id, t.priority, f.id AS file_id, f.path AS path "
            "FROM tasks t JOIN files f ON f.id = t.file_id "
            "WHERE t.status = 'pending' AND t.anchor_start_line IS NULL"
        ).fetchall()
        for r in rows:
            try:
                fpath = repo_root / r["path"]
                if not fpath.is_file():
                    continue
                text = fpath.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                total_lines = text.count("\n") + 1
                file_tokens = count_tokens(text)
                def_lines = _method_lines(client, r["path"])
                if not should_decompose(
                    file_tokens=file_tokens, fn_count=len(def_lines),
                    priority=r["priority"], cpg_available=True,
                ):
                    continue
                boundaries = decorator_aware_starts(lines, def_lines)
                slices = plan_slices(total_lines, boundaries, file_tokens)
            except Exception:  # noqa: BLE001
                # One pathological file — unreadable, a path the CPG validator
                # rejects (e.g. an '@' in a vendored JS asset), or a CPG error —
                # must never abort the whole expansion pass.
                continue
            if len(slices) <= 1:
                continue
            now = utc_now()
            for (s, e) in slices:
                conn.execute(
                    "INSERT INTO tasks (file_id, priority, status, "
                    "anchor_start_line, anchor_end_line, parent_task_id, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (r["file_id"], r["priority"], s, e, r["task_id"], now, now),
                )
            conn.execute(
                "UPDATE tasks SET status = 'decomposed', updated_at = ? WHERE id = ?",
                (now, r["task_id"]),
            )
            counts["files"] += 1
            counts["slices"] += len(slices)
    return counts
