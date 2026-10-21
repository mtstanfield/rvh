"""Joern Scala query builders for the rvh-cpg MCP tools.

Each function produces a self-contained Scala expression that, when
evaluated by ``joern --server``, yields a JSON string via ``.toJsonPretty``.

All dynamic inputs are validated/escaped — we never splat user input into
Scala unsanitized.

Notes on the Scala API in Joern 4.0.x
-------------------------------------
Direct property access on a traversal (e.g. ``m.name``) returns
``Iterator[T]``, not ``T``. This means:

  * ``where(m => m.name == "foo")`` no longer typechecks — ``where`` needs
    an ``Iterator[_]`` predicate.
  * ``filter(_.name == "foo")`` also fails because the RHS is an Iterator.

The idiomatic fix is either a dedicated traversal step
(``nameExact("foo")``, ``internal``, ``external``) or wrapping the property
access with ``headOption.contains(...)``/``.exists(...)`` inside ``filter``.
"""
from __future__ import annotations

import re

_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:<>,*&\s\.]{0,200}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_./\-]{1,500}$")


def _validate_symbol(name: str) -> str:
    """Reject symbols containing Scala-dangerous chars."""
    if not isinstance(name, str) or not _SYMBOL_RE.match(name):
        raise ValueError(f"invalid symbol name: {name!r}")
    return name


def _validate_file(path: str) -> str:
    if not isinstance(path, str) or not _FILE_RE.match(path):
        raise ValueError(f"invalid file path: {path!r}")
    return path


def _scala_str(s: str) -> str:
    """Safely quote a string for Scala raw-string-literal embedding."""
    if '"""' in s or "${" in s:
        raise ValueError(f"unsafe string for Scala embedding: {s!r}")
    for c in s:
        if c in ("\n", "\r") or ord(c) < 0x20:
            raise ValueError(f"unsafe string for Scala embedding: {s!r}")
    return f'raw"""{s}"""'


# Snippet helpers — shared projections used in multiple queries. Keeping
# these as strings (rather than f-string fragments inline) makes each
# builder readable and consistent.

_METHOD_ROW = (
    'Map('
    '"file" -> m.filename, '
    '"line" -> m.lineNumber.getOrElse(0), '
    '"name" -> m.fullName'
    ')'
)

_CALL_ROW = (
    'Map('
    '"file" -> c.file.name.headOption.getOrElse(""), '
    '"line" -> c.lineNumber.getOrElse(0), '
    '"name" -> c.name, '
    '"code" -> c.code'
    ')'
)

_NODE_ROW = (
    'Map('
    '"label" -> n.label, '
    '"code" -> n.code, '
    '"file" -> n.file.name.headOption.getOrElse(""), '
    '"line" -> n.lineNumber.getOrElse(0)'
    ')'
)


# Synthetic method names joern emits in language frontends that aren't
# real entry points — filter them out so a Python repo doesn't return
# thousands of `<module>`/`<body>`/`<fakeNew>` scaffolding rows.
_SYNTHETIC_METHOD_NAMES = (
    "<global>", "<module>", "<body>",
    "<fakeNew>", "<metaClassCallHandler>", "<metaClassAdapter>",
    "<lambda>",  # lambdas are often noise in entrypoint listings
)


def build_entrypoints(
    *, file: str | None = None, limit: int = 200
) -> str:
    """List internal entry points: non-external, non-synthetic methods.

    ``isPublic`` is not exposed as a direct boolean in current Joern; we
    treat *internal* (``isExternal == false``) + non-synthetic as the
    working definition of a worker-visible entry point. ``main`` is
    guaranteed to appear when present.

    Synthetic methods (`<module>`, `<body>`, `<fakeNew>`,
    `<metaClassCallHandler>`, etc.) are filtered out — they're joern
    scaffolding, not user-written entry points. For Python repos this
    is the difference between ~200 real handlers and ~3000 rows.

    Optional file filter narrows to a single file. Default limit caps
    at 200 entries to keep the tool response under the model's context
    budget; raise it (with caution) via the ``limit`` argument.
    """
    synthetic_list = ", ".join(_scala_str(n) for n in _SYNTHETIC_METHOD_NAMES)
    base = (
        f'cpg.method.internal'
        f'.filter(m => !Set({synthetic_list}).contains(m.name))'
    )
    if file:
        _validate_file(file)
        base += (
            f'.filter(_.file.name.exists(_.matches({_scala_str(".*" + file + ".*")})))'
        )
    return (
        f'{base}.take({int(limit)}).l'
        f'.map(m => {_METHOD_ROW}).toJsonPretty'
    )


def build_func(*, name: str) -> str:
    """Look up a function by name (bare identifier) or by fullName (colon form).

    C/C++ method ``fullName`` includes a signature suffix (e.g.
    ``main:int(int,char**)``), so if the caller passes a bare identifier we
    match on ``nameExact``; otherwise we match on ``fullNameExact``.
    """
    _validate_symbol(name)
    lookup = (
        f'cpg.method.fullNameExact({_scala_str(name)})'
        if ":" in name
        else f'cpg.method.nameExact({_scala_str(name)})'
    )
    return (
        f'{lookup}.l.map(m => Map('
        '"name" -> m.fullName, '
        '"file" -> m.filename, '
        '"line" -> m.lineNumber.getOrElse(0), '
        '"signature" -> m.signature, '
        '"numCallees" -> m.call.size, '
        '"numParameters" -> m.parameter.size'
        ')).toJsonPretty'
    )


def build_paths(*, src: str, dst: str, max_paths: int) -> str:
    """Taint-bearing paths between two methods' parameter sets.

    Note: ``reachableByFlows`` requires a dataflow engine; the queries here
    rely on Joern's default ``OssDataFlow`` overlay which is applied by
    ``joern-parse`` when --overlays default is used. If you pass a CPG
    without the overlay the call returns empty but compiles.
    """
    _validate_symbol(src)
    _validate_symbol(dst)
    src_lookup = (
        f'cpg.method.fullNameExact({_scala_str(src)})'
        if ":" in src
        else f'cpg.method.nameExact({_scala_str(src)})'
    )
    dst_lookup = (
        f'cpg.method.fullNameExact({_scala_str(dst)})'
        if ":" in dst
        else f'cpg.method.nameExact({_scala_str(dst)})'
    )
    return (
        f'def srcTrav = {src_lookup}.parameter;\n'
        f'def sinkTrav = {dst_lookup}.parameter;\n'
        f'sinkTrav.reachableByFlows(srcTrav).take({int(max_paths)}).l'
        '.map(p => Map("nodes" -> p.elements.map(e => Map('
        '"file" -> e.file.name.headOption.getOrElse(""), '
        '"line" -> e.lineNumber.getOrElse(0), '
        '"code" -> e.code)))).toJsonPretty'
    )


# Default taint-source regex for `build_reachable` when the caller doesn't
# supply one. Generic argv/env/stdin — covers C, Python `sys.argv`, Go
# `os.Getenv`, etc. Workers specify a framework-specific pattern via
# `sources_hint` (e.g. `request\\.GET|request\\.POST` for Django) to get
# tighter reachability — framework knowledge lives in the model/prompt, not
# in this codebase.
DEFAULT_SOURCE_PATTERN = (
    r"(?i)argv|getenv|fgets|fread|recv|read\(|gets\(|std::cin|scanf"
)


def build_reachable(
    *, sink_file: str, sink_line: int, sources_hint: str | None = None
) -> str:
    _validate_file(sink_file)
    pattern = sources_hint if sources_hint else DEFAULT_SOURCE_PATTERN
    # Guard against hostile regex injection — same safety rules as symbol
    # names. A bad regex just returns empty, but we still reject strings
    # that could break out of the Scala raw-string literal.
    for c in pattern:
        if c in ("\n", "\r") or ord(c) < 0x20:
            raise ValueError(f"invalid sources_hint: {pattern!r}")
    return (
        f'def sink = cpg.call.filter(_.lineNumber.exists(_ == {int(sink_line)}))'
        f'.filter(_.file.name.exists(_.matches({_scala_str(".*" + sink_file + ".*")})));\n'
        f'def sources = cpg.call.name({_scala_str(pattern)});\n'
        'sink.reachableByFlows(sources).take(20).l'
        '.map(p => Map("source_file" -> p.elements.head.file.name.headOption.getOrElse(""), '
        '"source_line" -> p.elements.head.lineNumber.getOrElse(0), '
        '"source_code" -> p.elements.head.code, '
        '"hops" -> p.elements.size)).toJsonPretty'
    )


def build_callers(*, symbol: str, depth: int, limit: int = 100) -> str:
    """Reverse call graph: which methods (transitively) call ``symbol``.

    ``depth`` caps the BFS expansion. ``limit`` caps the total rows
    returned after dedup. Defaults keep output under the model's
    context budget on large callgraphs.
    """
    _validate_symbol(symbol)
    depth = max(1, min(int(depth), 10))
    lookup = (
        f'cpg.method.fullNameExact({_scala_str(symbol)})'
        if ":" in symbol
        else f'cpg.method.nameExact({_scala_str(symbol)})'
    )
    return (
        f'{lookup}.repeat(_.caller)(_.maxDepth({depth}).emit).dedup'
        f'.take({int(limit)}).l'
        f'.map(m => {_METHOD_ROW}).toJsonPretty'
    )


def build_callees(*, symbol: str, depth: int, limit: int = 100) -> str:
    """Forward call graph: which methods ``symbol`` (transitively) calls."""
    _validate_symbol(symbol)
    depth = max(1, min(int(depth), 10))
    lookup = (
        f'cpg.method.fullNameExact({_scala_str(symbol)})'
        if ":" in symbol
        else f'cpg.method.nameExact({_scala_str(symbol)})'
    )
    return (
        f'{lookup}.repeat(_.callee)(_.maxDepth({depth}).emit).dedup'
        f'.take({int(limit)}).l'
        f'.map(m => {_METHOD_ROW}).toJsonPretty'
    )


def build_xref(*, symbol: str, limit: int = 100) -> str:
    """Every call + definition of ``symbol`` in the CPG, capped at ``limit``
    rows. Default 100 prevents popular names (e.g. ``get`` in a Django
    repo) from returning thousands of matches that blow the context.

    Calls are streamed BEFORE identifiers: a call node is a sink — the thing
    a security audit cares about — whereas an identifier is usually an import
    or a bare name reference. When ``take(limit)`` truncates, calls must
    survive. Otherwise a symbol with many module-level identifier hits
    (``from x import eval`` repeated across a dozen scripts) can push the real
    ``eval(...)`` call-sites past the cap and hide them entirely — which is
    exactly how a confirmed ``eval``/``Popen`` sink can read as "not found."

    A qualified symbol (``subprocess.run``, ``std::system``) can't be matched
    on ``name``: joern stores only the short name there (``run``), with the
    qualified form in ``code`` as written and in ``methodFullName`` as
    resolved. So calls match on the short name and are kept when either
    field carries the qualified symbol; identifiers are skipped, as they're
    bare names and would only add unrelated ``run`` hits.
    """
    _validate_symbol(symbol)
    short = re.split(r"::|\.", symbol)[-1]
    if short == symbol:
        return (
            f'(cpg.call.name({_scala_str(symbol)}) ++ '
            f'cpg.identifier.name({_scala_str(symbol)}))'
            f'.take({int(limit)}).l'
            f'.map(n => {_NODE_ROW}).toJsonPretty'
        )
    qualified = _scala_str(symbol)
    return (
        f'cpg.call.nameExact({_scala_str(short)})'
        f'.filter(c => c.code.contains({qualified}) || c.methodFullName.contains({qualified}))'
        f'.take({int(limit)}).l'
        f'.map(n => {_NODE_ROW}).toJsonPretty'
    )


def build_inspect(*, file: str, line: int) -> str:
    """List nodes (calls, identifiers, literals, methods) at ``file:line``.

    ``cpg.all`` yields ``StoredNode`` which lacks the ``code``/``lineNumber``
    projections we need; we union the four concrete node types that carry
    them.
    """
    _validate_file(file)
    name_pat = _scala_str(".*" + file + ".*")
    line_lit = int(line)

    def _node_stream(step: str) -> str:
        return (
            f'cpg.{step}'
            f'.filter(_.file.name.exists(_.matches({name_pat})))'
            f'.filter(_.lineNumber.exists(_ == {line_lit}))'
            f'.map(n => {_NODE_ROW})'
        )

    return (
        f'({_node_stream("call")} ++ '
        f'{_node_stream("identifier")} ++ '
        f'{_node_stream("literal")} ++ '
        f'{_node_stream("method")}'
        ').l.toJsonPretty'
    )
