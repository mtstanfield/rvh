#!/usr/bin/env python3
"""MCP server exposing rvh-cpg operations as distinct tools.

Launched by goose via `goose run --with-extension "python3 /opt/rvh/cpg_mcp_server.py"`.
Uses stdio transport; the model calls them as native tools rather than
through shell-command text.

Each tool wraps an existing cpg_* function — no new logic, just distinct
schemas with descriptions that guide the model's tool choice.

CRITICAL: stdio MCP servers must NOT write to stdout (reserved for the MCP
protocol). Diagnostics go to stderr only.
"""
from __future__ import annotations

import functools
import os
import re
import sys
import time
from pathlib import Path

# Make sibling modules importable when launched from /opt/rvh/
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import cpg_formatters
import cpg_queries
from cpg_client import CpgClient, CpgQueryError


mcp = FastMCP("rvh-cpg")


# stdio MCP servers must not write to stdout. Stderr works but goose doesn't
# tee MCP subprocess stderr into its own transcript, so we ALSO write to a
# bind-mounted file inside the container. /output is the task's directory
# on the host, so the log lands at .rvh-runs/tasks/<task>/cpg-mcp.log.
_LOG_PATH = os.environ.get("RVH_CPG_MCP_LOG", "/output/cpg-mcp.log")
_LOG_FILE = None


def _get_log_file():
    """Lazy-open the file log; returns None if the path isn't writable."""
    global _LOG_FILE
    if _LOG_FILE is not None:
        return _LOG_FILE
    try:
        # line-buffered so multi-worker appends interleave cleanly per-line
        _LOG_FILE = open(_LOG_PATH, "a", buffering=1)
    except Exception:
        _LOG_FILE = None  # sentinel: don't retry
    return _LOG_FILE


def _log(tool_name: str, sig: str, result: str) -> None:
    """Write a one-line diagnostic to stderr + the bind-mounted log file.

    stdout is reserved for the MCP protocol — we MUST NOT print there.
    pid + timestamp let multi-worker runs be grep-separable.
    """
    preview = result[:80].replace("\n", "  ")
    extras = f" bytes={len(result)}"
    line = (
        f"[rvh-cpg-mcp pid={os.getpid()} ts={time.strftime('%H:%M:%S')}] "
        f"{tool_name}({sig}){extras} preview={preview!r}\n"
    )
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
    f = _get_log_file()
    if f is not None:
        try:
            f.write(line)
        except Exception:
            pass


# Max bytes a single tool response may occupy in the model's context.
# 16KB ≈ 4000 tokens; on a 256K-context model that's under 2% — small
# enough that a wide-fanout call (entrypoints on a large Django repo,
# xref on a common name like `get`) can't dominate the context window.
# Workers that need more can use narrower `file=` or tighter `limit=`
# filters; this cap is a last-resort safety rail, not the primary knob.
MAX_TOOL_OUTPUT_BYTES = 16384


def _cap_output(result: str) -> str:
    """Truncate oversized tool results and append a machine-readable note."""
    if not isinstance(result, str) or len(result) <= MAX_TOOL_OUTPUT_BYTES:
        return result
    head = result[:MAX_TOOL_OUTPUT_BYTES]
    dropped_bytes = len(result) - MAX_TOOL_OUTPUT_BYTES
    # Count lines in the original so the model knows how much was cut.
    total_lines = result.count("\n") + (0 if result.endswith("\n") else 1)
    return (
        head
        + f"\n\n(truncated: {dropped_bytes} more bytes / "
        f"~{total_lines} total lines. "
        "Re-issue with a tighter `file=` filter or a lower `limit` to "
        "see the tail, or narrow via `xref`/`callers` on a specific "
        "symbol instead of a broad sweep.)"
    )


# Per-session dedup cache: {(tool_name, arg_tuple): (count, cached_result)}.
# Lives in the MCP server subprocess (one per worker container) — cache
# lifetime == worker lifetime. When a model calls a tool with identical
# args to a prior call this session, we return the cached result with a
# "duplicate call" note, bypassing the sidecar round-trip and nudging the
# model toward progress. Models tend to re-verify settled results; this
# is the antidote, complementing goose's
# `max-tool-repetitions` which only bites after the 50th repeat.
_CALL_CACHE: dict[tuple, tuple[int, str]] = {}


def _call_key(args: tuple, kwargs: dict) -> tuple:
    """Canonicalize (args, kwargs) into a hashable key for the dedup cache."""
    # Our tool args are all scalars (str/int/None) so repr-per-arg is a
    # safe, deterministic key. Sort kwargs for order-independence.
    return tuple(repr(a) for a in args) + tuple(
        sorted((k, repr(v)) for k, v in kwargs.items())
    )


def _logged(tool_name: str):
    """Decorator: cap output + dedup identical calls + log each invocation."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (tool_name, _call_key(args, kwargs))
            parts = [repr(a)[:60] for a in args]
            parts.extend(f"{k}={v!r}"[:60] for k, v in kwargs.items())
            sig = ", ".join(parts)[:160]

            cached = _CALL_CACHE.get(key)
            if cached is not None:
                prior_count, prior_result = cached
                new_count = prior_count + 1
                _CALL_CACHE[key] = (new_count, prior_result)
                note = (
                    f"(duplicate call #{new_count} — same arguments as a "
                    "prior call this session. Identical tool calls return "
                    "identical results; the prior output is reproduced "
                    "below. Move on: ask a DIFFERENT question, or if you "
                    "have enough evidence, write the report.)\n\n"
                )
                result = _cap_output(note + prior_result)
                try:
                    _log(tool_name, sig + f" [dup#{new_count}]", result)
                except Exception:
                    pass
                return result

            result = fn(*args, **kwargs)
            result = _cap_output(result)
            if isinstance(result, str):
                _CALL_CACHE[key] = (1, result)
            try:
                _log(tool_name, sig, result if isinstance(result, str) else repr(result))
            except Exception:
                pass  # logging must never break the tool
            return result
        return wrapper
    return decorator


def _clear_call_cache() -> None:
    """Test-only hook: reset the per-session dedup cache."""
    _CALL_CACHE.clear()


def _make_client() -> CpgClient:
    endpoint = os.environ.get("RVH_CPG_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError(
            "RVH_CPG_ENDPOINT not set — no CPG sidecar available"
        )
    return CpgClient(endpoint)


def _safe_call(fn, *args, **kwargs) -> str:
    """Run a cpg_* call and return the formatted result, or a clean error
    string the model can handle without crashing."""
    try:
        return fn(*args, **kwargs)
    except CpgQueryError as e:
        return f"CPG query failed: {e}"
    except Exception as e:
        return f"CPG tool error: {type(e).__name__}: {e}"


# Tool-output chaining: when a CPG result is empty, append a hint about the
# NEXT tool to call. This keeps composition reactive (hint depends on what
# the tool actually returned) rather than statically planned. Models read
# the appended hint and tend to copy the suggested call verbatim.
def _hint_empty_reachable(sink: str) -> str:
    return (
        "\n(Hint: CPG's source set is argv/env/network/stdin/fread — it "
        "doesn't cross C-API boundaries. If this file is library code "
        "called from other compilation units, \"no reaching source\" "
        "does not mean safe. Try:\n"
        "  - callers(symbol=\"<function-enclosing-the-sink>\") to find "
        "callers of the sink's enclosing function\n"
        "  - xref(symbol=\"<sink-call-name>\") to find all defs+uses)\n"
    )


def _hint_empty_paths(src: str, dst: str) -> str:
    return (
        f"\n(Hint: no direct CPG path from {src!r} to {dst!r}. If these "
        "symbols are called across compilation units, CPG may miss the "
        "linkage. Try:\n"
        f"  - callers(symbol=\"{dst}\") to find call sites of the sink\n"
        f"  - xref(symbol=\"{src}\") to find where the source symbol appears\n"
        "Then manually verify the flow by reading the intermediate code.)\n"
    )


def _hint_empty_callers(symbol: str) -> str:
    return (
        f"\n(Hint: no in-CPG callers for {symbol!r}. The function may be "
        "exported via a C API wrapper, only called externally, or named "
        "differently in the CPG. Try:\n"
        f"  - xref(symbol=\"{symbol}\") to find any mention of the symbol\n"
        f"  - func(name=\"{symbol}\") to confirm the exact name/signature)\n"
    )


def _hint_empty_callees(symbol: str) -> str:
    return (
        f"\n(Hint: no in-CPG callees for {symbol!r}. The function may be "
        "a leaf (no further calls), inline, or the name differs from what "
        "CPG indexed. Try:\n"
        f"  - func(name=\"{symbol}\") to confirm the exact name/signature)\n"
    )


def _hint_empty_xref(symbol: str) -> str:
    # The CPG is not exhaustive (joern's Go frontend, for one, drops calls
    # inside closures), so an empty result must not read as "unused".
    short = re.split(r"::|\.", symbol)[-1]
    return (
        f"\n(Hint: the CPG has no reference to {symbol!r}, but it can miss "
        "real calls (e.g. inside closures or lambdas). Confirm with a text "
        f"search — `rg -n '{short}'` — before concluding it is unused.)\n"
    )


def _hint_empty_func(name: str) -> str:
    return (
        f"\n(Hint: function {name!r} not found in the CPG. Try the "
        "fully-qualified form (e.g. \"MyClass::method\") or use "
        f"xref(symbol=\"{name}\") to find where the name appears.)\n"
    )


# Non-empty hints: when a tool returned real data, suggest paths() as the
# follow-up to actually trace data flow. The empty hints pivot to
# callers/xref; these complete the chain by steering to paths once we
# know something.
def _hint_nonempty_callers(symbol: str) -> str:
    return (
        f"\n(Next step: callers of {symbol!r} identified above. If data "
        "might flow from a parser/network-read/external-source through "
        f"one of these callers into {symbol}, trace it with:\n"
        f"  paths(src=\"<parser-or-source-symbol>\", dst=\"{symbol}\")\n"
        "Common src candidates: \"picojson::parse\", \"json::parse\", "
        "\"curl_easy_perform\", \"read\", \"recv\".)\n"
    )


def _hint_nonempty_reachable(sink: str) -> str:
    return (
        f"\n(Next step: sources DO reach {sink}. For the full hop chain "
        "(each intermediate code step, not just the hop count), trace "
        "with:\n"
        f"  paths(src=\"<symbol-from-source-code-column-above>\", "
        f"dst=\"<symbol-at-{sink}>\")\n"
        "Verify whether any hop sanitizes or validates the data before "
        "it reaches the sink.)\n"
    )


@_logged("paths")
def _tool_paths(src: str, dst: str, max_paths: int = 10) -> str:
    client = _make_client()
    scala = cpg_queries.build_paths(src=src, dst=dst, max_paths=max_paths)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):  # error string from _safe_call
        return rows
    result = cpg_formatters.format_paths(rows)
    if not rows:
        result += _hint_empty_paths(src, dst)
    return result


@_logged("reachable")
def _tool_reachable(sink: str, sources_hint: str | None = None) -> str:
    client = _make_client()
    if ":" not in sink:
        return f"reachable: expected FILE:LINE, got {sink!r}"
    file, line_s = sink.rsplit(":", 1)
    try:
        line = int(line_s)
    except ValueError:
        return f"reachable: invalid line number in {sink!r}"
    try:
        scala = cpg_queries.build_reachable(
            sink_file=file, sink_line=line, sources_hint=sources_hint
        )
    except ValueError as e:
        return f"reachable: {e}"
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    result = cpg_formatters.format_reachable(rows)
    if not rows:
        result += _hint_empty_reachable(sink)
    else:
        result += _hint_nonempty_reachable(sink)
    return result


@_logged("callers")
def _tool_callers(symbol: str, depth: int = 3, limit: int = 100) -> str:
    client = _make_client()
    scala = cpg_queries.build_callers(symbol=symbol, depth=depth, limit=limit)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    result = cpg_formatters.format_callers(rows)
    if not rows:
        result += _hint_empty_callers(symbol)
    else:
        result += _hint_nonempty_callers(symbol)
    return result


@_logged("callees")
def _tool_callees(symbol: str, depth: int = 3, limit: int = 100) -> str:
    client = _make_client()
    scala = cpg_queries.build_callees(symbol=symbol, depth=depth, limit=limit)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    result = cpg_formatters.format_callees(rows)
    if not rows:
        result += _hint_empty_callees(symbol)
    return result


@_logged("xref")
def _tool_xref(symbol: str, limit: int = 100) -> str:
    client = _make_client()
    scala = cpg_queries.build_xref(symbol=symbol, limit=limit)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    result = cpg_formatters.format_xref(rows)
    if not rows:
        result += _hint_empty_xref(symbol)
    return result


@_logged("func")
def _tool_func(name: str) -> str:
    client = _make_client()
    scala = cpg_queries.build_func(name=name)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    if not rows:
        return cpg_formatters.format_func(rows) + _hint_empty_func(name)
    return cpg_formatters.format_func(rows)


@_logged("inspect")
def _tool_inspect(location: str) -> str:
    client = _make_client()
    if ":" not in location:
        return f"inspect: expected FILE:LINE, got {location!r}"
    file, line_s = location.rsplit(":", 1)
    try:
        line = int(line_s)
    except ValueError:
        return f"inspect: invalid line number in {location!r}"
    scala = cpg_queries.build_inspect(file=file, line=line)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    return cpg_formatters.format_inspect(rows)


@_logged("entrypoints")
def _tool_entrypoints(file: str | None = None, limit: int = 200) -> str:
    client = _make_client()
    scala = cpg_queries.build_entrypoints(file=file, limit=limit)
    rows = _safe_call(client.query, scala)
    if isinstance(rows, str):
        return rows
    return cpg_formatters.format_entrypoints(rows)


@mcp.tool()
def paths(src: str, dst: str, max_paths: int = 10) -> str:
    """Trace taint paths between two symbols. THE primary evidence tool
    for a defense claim.

    Interpreting results:
      - paths returned → a flow exists; you must identify a hop that
        sanitizes/validates, or the defense is incomplete.
      - no paths → defense by unreachability (BUT: `src`/`dst` may be
        named differently than what CPG indexed; if you're not sure,
        confirm with `xref(dst)` before claiming unreachable).

    Args:
        src: Source symbol (function name or fully-qualified name).
        dst: Sink symbol.
        max_paths: Max number of distinct paths to return (default 10).
    """
    return _tool_paths(src, dst, max_paths)


@mcp.tool()
def reachable(sink: str, sources_hint: str | None = None) -> str:
    """Which attacker-controllable sources reach this FILE:LINE sink?

    Uses Joern's taint engine to trace flows from source calls to the
    sink. The source set comes from `sources_hint` when provided,
    otherwise a generic default (argv/env/stdin/recv) — which only
    covers C-style entry. **For Python/Go/Java/PHP repos pass a
    `sources_hint` regex that matches the framework's request objects**,
    e.g. `request\\\\.GET|request\\\\.POST` for Django, `request\\\\.args`
    for Flask, `HttpServletRequest` for Java Servlet, `r\\\\.URL\\\\.Query`
    for Go net/http, `\\\\$_GET|\\\\$_POST` for PHP.

    Interpreting results:
      - sources listed → chase the full hop chain with `paths(src, dst)`.
      - no reaching source → MAY mean safe, but only if the sink is in
        a main()-reachable path. For library code called from outside
        the CPG's source model, "no reaching source" is just a CPG
        blind spot; confirm coverage with `callers(enclosing_function)`
        before claiming safety. Also retry with a tighter/looser
        `sources_hint` if the default didn't match the repo's idioms.

    Args:
        sink: Location in FILE:LINE form, e.g. `src/foo.cpp:199`.
        sources_hint: Optional regex matching attacker-source call
            names for this repo. Case-insensitive; applied to call
            `.name`. Omit for the generic C-style default.
    """
    return _tool_reachable(sink, sources_hint)


@mcp.tool()
def callers(symbol: str, depth: int = 3, limit: int = 100) -> str:
    """Reverse call graph: who invokes this function?

    The right follow-up when `reachable`/`paths` returned empty —
    confirms whether the sink's enclosing function has ANY callers CPG
    knows about. No callers = CPG blind spot (C-API entry, function
    pointer, template); pivot to `xref(symbol)` to find how the symbol
    is actually referenced.

    Args:
        symbol: Function name or fully-qualified name.
        depth: How many levels of callers to return (default 3).
        limit: Max caller rows returned after dedup (default 100). Raise
            only if you've seen a truncation note in a prior result.
    """
    return _tool_callers(symbol, depth, limit)


@mcp.tool()
def callees(symbol: str, depth: int = 3, limit: int = 100) -> str:
    """Forward call graph: what this function invokes.

    Args:
        symbol: Function name or fully-qualified name.
        depth: How many levels of callees to return (default 3).
        limit: Max callee rows returned after dedup (default 100).
    """
    return _tool_callees(symbol, depth, limit)


@mcp.tool()
def xref(symbol: str, limit: int = 100) -> str:
    """Every definition and use of a symbol across the codebase.

    The escape hatch when `callers`/`paths`/`reachable` come up empty —
    it'll find the symbol anywhere it appears, even in contexts CPG
    can't connect via call edges (templates, function pointers, macros,
    header-only declarations). Beware: common names (e.g. `get` in a
    Django repo, `call` in Java) match thousands of nodes; when you see
    a truncation note, switch to a more specific qualified form.

    Args:
        symbol: Symbol name (variable, struct member, function).
        limit: Max rows returned (default 100).
    """
    return _tool_xref(symbol, limit)


@mcp.tool()
def func(name: str) -> str:
    """Function summary: signature, file location, number of callees.

    Sniff test before reading a body — tells you if the function is
    small enough to read straight or whether to narrow via `callees`
    first. Returns "function not found" for names CPG didn't index; if
    that happens, `xref(name)` will still locate references.

    Args:
        name: Function name or fully-qualified name.
    """
    return _tool_func(name)


@mcp.tool()
def inspect(location: str) -> str:
    """What's at this specific code point? (call, assignment, condition,
    etc.) Plus whether CPG classifies it as a sink.

    Args:
        location: FILE:LINE form, e.g. `src/foo.cpp:199`.
    """
    return _tool_inspect(location)


@mcp.tool()
def entrypoints(file: str | None = None, limit: int = 200) -> str:
    """List internal entry points (main, handlers, class methods).

    Use once per investigation to orient — these are where untrusted
    input typically enters the repo. Synthetic joern scaffolding
    (`<module>`, `<body>`, `<fakeNew>`, etc.) is filtered out so
    dynamic-language CPGs don't drown you in wrappers.

    On large repos the unfiltered result can be huge; pass ``file=``
    to narrow to a specific source file, or lower ``limit=`` if you
    only need a sample to confirm the CPG is queryable.

    Args:
        file: Optional — restrict to a single file
            (e.g. ``gui/views.py``). Prefer this over unfiltered sweeps
            on repos with hundreds of files.
        limit: Max entries returned (default 200).
    """
    return _tool_entrypoints(file, limit)


def registered_tool_names() -> list[str]:
    """Return the names of all tools registered on the MCP server.

    FastMCP stores tools on a manager; this accessor stays compatible if
    internals shift (we fall back to listing attributes prefixed with
    `_tool_`).
    """
    tm = getattr(mcp, "_tool_manager", None)
    if tm is not None and hasattr(tm, "list_tools"):
        tools = tm.list_tools()
        return [t.name for t in tools]
    return [
        attr[len("_tool_"):]
        for attr in globals()
        if attr.startswith("_tool_") and callable(globals()[attr])
    ]


if __name__ == "__main__":
    # Stdio transport — stdout is the MCP protocol, stderr is ours.
    mcp.run(transport="stdio")
