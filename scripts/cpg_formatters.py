"""Format Joern JSON query results as tab-separated text for the worker.

All formatters emit a header row + data rows, tab-separated, so the worker
can consume them with awk or direct reading. Consistent "file:line" as the
first column across all commands.
"""
from __future__ import annotations

from typing import Sequence, Mapping, Any


def _row(fields: list[str]) -> str:
    return "\t".join(fields)


def _loc(r: Mapping[str, Any]) -> str:
    return f"{r.get('file', '')}:{r.get('line', 0)}"


def format_paths(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no path found\n"
    out = []
    for i, p in enumerate(rows, 1):
        nodes = p.get("nodes", [])
        out.append(f"Path {i}:")
        for n in nodes:
            out.append(f"  {_loc(n)}\t{n.get('code', '')}")
        out.append("")
    return "\n".join(out)


def format_reachable(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no reaching source\n"
    header = _row(["source_file:line", "hops", "code"])
    body = "\n".join(
        _row([f"{r.get('source_file','')}:{r.get('source_line', 0)}",
              str(r.get("hops", 0)),
              r.get("source_code", "")])
        for r in rows
    )
    return header + "\n" + body + "\n"


def format_callers(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no callers\n"
    header = _row(["file:line", "symbol"])
    body = "\n".join(_row([_loc(r), r.get("name", "")]) for r in rows)
    return header + "\n" + body + "\n"


def format_callees(rows: Sequence[Mapping[str, Any]]) -> str:
    return format_callers(rows)  # same shape


def format_xref(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no references\n"
    header = _row(["file:line", "kind", "code"])
    body = "\n".join(
        _row([_loc(r), r.get("kind", ""), r.get("code", "")]) for r in rows
    )
    return header + "\n" + body + "\n"


def format_func(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "function not found\n"
    r = rows[0]
    return (
        f"name\t{r.get('name', '')}\n"
        f"location\t{_loc(r)}\n"
        f"signature\t{r.get('signature', '')}\n"
        f"numCallees\t{r.get('numCallees', 0)}\n"
        f"numParameters\t{r.get('numParameters', 0)}\n"
    )


def format_inspect(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no node at that location\n"
    header = _row(["kind", "code"])
    body = "\n".join(_row([r.get("label", ""), r.get("code", "")]) for r in rows)
    return header + "\n" + body + "\n"


def format_entrypoints(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no entrypoints found\n"
    header = _row(["file:line", "symbol"])
    body = "\n".join(_row([_loc(r), r.get("name", "")]) for r in rows)
    return header + "\n" + body + "\n"


def format_ambiguous_symbol(*, symbol: str, candidates: Sequence[str]) -> str:
    out = [f"ambiguous symbol '{symbol}' — candidates:"]
    for c in candidates:
        out.append(f"  {c}")
    out.append("(re-invoke with a qualified name)")
    return "\n".join(out) + "\n"
