# CPG availability — enumeration-first guidance

The CPG MCP tools (`paths`, `reachable`, `callers`, `callees`, `xref`,
`func`, `inspect`, `entrypoints`) are your **primary enumeration**
tools for this anchor. CPG indexes call nodes that shell grep can
miss — aliased imports (`from x import y as z`), decorator stacks,
dynamic dispatch, generated code. Use CPG to find sinks; use shell to
read what's there.

**Standard opening sequence on any anchor:**

1. `entrypoints(file=<anchor>)` — every routed handler in the anchor
   file. Surfaces handler-shape directly and tells you what auth
   gates each handler claims via decorator metadata.
2. `xref(symbol=<call>)` for each dangerous call in the language
   module's bug-class list. Joern stores call nodes by short name
   (`eval`, `execute`, `loads`, `get_or_create`, `set_password`,
   `setattr`), so search the unqualified function name. When the
   language module ships a CPG-query battery, run it before
   committing to a verdict.
3. For each hit `xref` returned, `inspect(location=<file:line>)`
   to see the AST shape and surrounding code without paging the
   whole file via shell.

**Walking a candidate sink:**

- `reachable(sink=<file:line>, sources_hint=<framework-pattern>)`
  to confirm an attacker-controlled source actually reaches the
  sink. A sink with no reachable source is non-boundary.
- `paths(src=<source>, dst=<sink>)` for the source-to-sink walk
  you'll cite as evidence in the final report.
- `callers(symbol=<call>)` / `callees(symbol=<call>)` when sketching
  call structure, or when `reachable`/`paths` came back empty — the
  symbol may be CPG-blind (function pointer, C-extension, dynamic
  attribute), and the right pivot is `xref` on a related symbol.

**Path convention.** CPG stores repo-relative paths. Expect
`src/foo.py:42`, not `/workspace/src/foo.py:42`. When you cite a
location in your final report, prefer the repo-relative form CPG
returned (it matches the human reviewer's working-copy layout).

**Handler-shape sanity check.** When the anchor matches handler shape
(≥3 routed-handler matches in its first 200 lines — `def <name>(request, ...)`,
`class <Name>(View|...)`, `@app.<verb>`, `@<verb>Mapping`,
`func <Name>(c *gin.Context)`, `@RestController`, etc.) AND the
framework hints name a web framework, you MUST run the full CPG
enumeration battery from the language module **before any
`final_output`** — not just before no-finding. Surfacing one finding
does not release you from enumerating other sink categories on the
same file. The canonical false negative is the second finding that
lived in a different category from the first and got missed because
`final_output` landed too early.
