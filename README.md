# rvh

A practical replication of the methodology from Anthropic's Mythos-preview red-team paper: automated defensive vulnerability hunting with LLMs, producing maintainer-verifiable candidate findings rather than exploits.

> **Read the paper before running this.** The Mythos work established the threat model, the class of findings this approach produces, and the failure modes we're defending against. Without that framing, results from this tool are easy to misread — especially the false-positive patterns a reviewer has to filter out.
>
> Paper: https://red.anthropic.com/2026/mythos-preview/

## What it does

rvh is a *recall* layer: it finds and ranks candidate vulnerabilities for a reviewer (a person, or a stronger model) to confirm. One run against a target repository:

1. **Inventory** — walk the repo and classify files by language and role; vendored code is set aside (`--include-vendored` scans it).
2. **Analyze and score** — the model classifies the repo layout (directories it calls tests, docs, CI, tooling or generated score 1), then scores each remaining file 1–5 for security relevance (`docs/reference/scoring-rubric.md`).
3. **Worker image** — one pinned image (goose, ripgrep, the CPG tools), built once per rvh version.
4. **Code property graph** — joern builds a CPG in parallel; large high-priority files are split into function-aligned regions so each gets a full worker pass.
5. **Workers** — for each file or region at or above `--min-priority`, a containerized goose agent reads the code, enumerates sinks, checks their defenses, and returns a structured report.
6. **Candidates** — reports land in `<target>/.rvh-runs/reports/`, indexed by severity in `<target>/.rvh-runs/CANDIDATES.md`.

Findings are candidates, not verdicts. Review them with a stronger model using `docs/reference/codex-triage-report-prompt.md`.

## Quick start

**Prerequisites:** Docker, Python 3.11+, and an OpenAI-compatible endpoint serving a capable model.

```bash
git clone <this repository> && cd rvh
export RVH_API_KEY=...   # omit for local servers that don't check keys
./rvh --repo /path/to/target --endpoint https://llm.example.com/v1 --model <model>
```

`./rvh` creates `.venv/` on first use, runs `pip install -e .`, then starts the run. A run lives in `<target>/.rvh-runs/` and resumes where it stopped; `--fresh` starts over. Ctrl+C stops gracefully: in-flight tasks are requeued for the next run.

## Sandbox

Workers run in containers on a per-run Docker network with no route out. They can reach two things: the run's LLM gateway, a small proxy that forwards to `--endpoint` and holds the API key (workers never see it), and the CPG sidecar. The target repository is mounted read-only, and a worker writes only to its own task directory.

**macOS with a LAN endpoint:** the gateway container has to reach the endpoint, so allow your container runtime (OrbStack or Docker Desktop) under System Settings → Privacy & Security → Local Network. rvh stops with this hint if the gateway can't connect.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--repo PATH` | required | Target repository |
| `--endpoint URL` | required | OpenAI-compatible endpoint, with or without `/v1` |
| `--model NAME` | required | Model used for every stage |
| `--api-key KEY` | `$RVH_API_KEY`, else `dummy` | Prefer the environment variable; flags show up in `ps` and shell history |
| `--min-priority N` | `3` | Only dispatch workers for files scored at least N (1–5) |
| `--parallelism N` | `1` | Concurrent workers |
| `--max-tasks N` | unlimited | Stop after N worker tasks |
| `--fresh` | off | Delete `<target>/.rvh-runs/` and start over |
| `--include PATH` | whole repo | Only scan under PATH (repeatable) |
| `--exclude PATH_OR_GLOB` | — | Skip a path, or files matching a glob containing `*`, `?` or `[` (repeatable) |
| `--include-vendored` | off | Scan vendored code too |
| `--no-cpg` | off | Skip the joern code property graph |
| `--debug` | off | Save stage prompts and outputs under `.rvh-runs/logs/debug/` |

`RVH_CPG_HEAP_GB` (default 16) sizes the joern JVM heap for large code property graphs; keep it within the memory your container runtime is allotted.
## CPG (Code Property Graph) tooling

Workers have access to Joern-backed source→sink navigation through 8 MCP tools exposed inside the container: `paths`, `reachable`, `callers`, `callees`, `xref`, `func`, `inspect`, `entrypoints`. These are the mechanism the worker uses to verify or refute the source→sink chain for each candidate finding. Dataflow traces (`paths(src, dst)`) require Joern's default overlay; the pipeline applies it automatically for every supported language.

**Supported languages** (12): C, C++, Python, Go, Java, JavaScript (including Vue components), TypeScript, PHP, Ruby, Kotlin, C#, Swift. The dominant code extension in the inventory determines which Joern frontend runs; override not currently exposed but straightforward to add.

**How it plugs in:** After the inventory phase, the pipeline builds a CPG with a joern image pinned by digest (cached per repo, rebuilt when the source hash changes), starts a `joern --server` sidecar on the run's network, and workers query it through the `rvh-cpg` MCP server inside their container. The build runs on a background thread while analyze and score run, so it's usually free on the critical path.

**Prerequisites:** a Docker daemon. The first CPG build pulls the pinned joern image (about 1 GB) if it's missing; to fetch it ahead of time:

```bash
docker pull ghcr.io/joernio/joern@sha256:869aa86c27609d78d1ca4a48b88304bcfd7681f85f4f7ac6557a7aff16a2960d
```

The digest is `JOERN_IMAGE` in `scripts/cpg_build.py`; `:nightly` is a different image.

**Per-language worker guidance** lives at `prompts/worker_<lang>_cpg.md` — short cheatsheets with language-specific sink/source idioms (e.g. Django `request\.GET|request\.POST` for `reachable()`'s `sources_hint`). The base `prompts/worker_cpg.md` sets the tool-use discipline: the CPG to enumerate sinks, the shell to read the code.

**Disable with** `--no-cpg` to run shell-only workers.

## Known limitations

This tool produces real findings and has real gaps. Calibrate accordingly.

**Multi-hop taint flows with CPG-opaque segments remain under-detected.** The CPG tooling closed the single-hop gap — workers now routinely trace attacker input through one or two call edges using `paths()`/`reachable()`. The remaining gaps are where Joern's language frontend can't model the flow:

- **C-API shims and function pointers** (scitokens-cpp's `scitoken_deserialize` → C++ class method): `paths()` returns no path even though the code clearly connects them. Worker must fall back to `xref` + manual reading.
- **Framework magic**: Django middleware chains, Spring AOP autowiring, Rails callbacks, FastAPI dependency injection — edges implicit in decorators/annotations that joern doesn't see.
- **Reflection / dynamic dispatch**: Java `Method.invoke`, Python `getattr`, Ruby `send`.
- **Cross-language boundaries**: frontend JS posts JSON → backend Python deserializes. Each language has its own CPG island; no tool exists to traverse the boundary.

Reference example: the OAuth/OIDC-style SSRF pattern where an attacker-controlled issuer claim drives a metadata fetch whose response contains a `jwks_uri` field that drives a second fetch. The `paths()` tool can confirm the first-hop flow when it lives within one CPG; the across-a-C-API-boundary variant (e.g. scitokens-cpp's C-to-C++ wrappers) still shows as no-path.

**Practical guidance.** When the worker reports `no-finding` on a URL-fetching or deserialization sink and the CPG tools returned empty, check whether the code uses any of the opaque patterns above. If so, CPG silence is inconclusive; manual review of the specific call chain is warranted.

**Other honest gaps:**

- **Budget exhaustion on high-sink-count anchors.** Files with 100+ enumerated sinks (typically large auth/identity modules) can use up the worker's wall-clock budget (60 minutes at priority 5, 30 below) before the defense checks finish. A killed worker is retried, up to three attempts; when its last turn holds an analysis, the retry only writes that analysis up as a report, so sinks it hadn't checked stay unchecked. The run log marks these with `budget kill — stashed salvage sidecar`; those anchors warrant manual review.
- **CWE labels on non-SQL query-language injections.** The worker sometimes picks CWE-89 (SQL Injection) for bugs that are really CWE-917 (Expression Language Injection) or CWE-200 (Information Exposure). The underlying bug description is usually correct; triage re-codes.
- **Fortran is unsupported.** No worker prompt module exists for Fortran. HEP/CMSSW codebases mixing C++ and Fortran will have the Fortran portions effectively skipped.
- **Custom binary protocol parsers.** Generic worker prompts don't know about HEP file formats, scheduler protocols, or domain-specific binary IPC. Bugs in those parsers are in the gap.

## Architecture pointers (for agents reading this repo)

If you (or a coding agent) are reading the code to understand it, these are the load-bearing files in roughly the order to read them:

- `scripts/run_repo_vuln_hunt.py` — entry point; parses the CLI, runs preflight, builds the worker image, and runs bootstrap (the CPG build starts on a background thread once the inventory is done, while analyze and score run). Then it starts the LLM gateway and runs the engine.
- `scripts/run_autonomous_v2.py` — the engine; dispatches workers in priority order, ingests their reports, and retries or requeues failed attempts.
- `scripts/worker.py` — how worker prompts are built, how containers are launched, how goose is invoked (`build_goose_inner_args`, the `GOOSE_SYSTEM_PROMPT_FILE_PATH` write).
- `scripts/image.py` + `docker/worker.Dockerfile` — the one worker image, tagged by a hash of the Dockerfile and every file it copies.
- `scripts/cpg_build.py`, `scripts/cpg_sidecar.py`, `scripts/cpg_mcp_server.py`, `scripts/cpg_queries.py` — CPG build orchestration, sidecar lifecycle, the stdio MCP server workers see, and the Scala query builders.
- `prompts/worker.md` — the base prompt that drives every worker session. Read this before claiming the tool "doesn't find X" — the prompt is the tool.
- `prompts/worker_cpg.md` + `worker_<lang>_cpg.md` — CPG tool-use discipline (base) and per-language cheatsheets (python/go/java/php/ruby/js/ts, plus cpp). Loaded automatically based on the anchor file's extension.
- `.rvh-runs/state.db` — SQLite; `files`, `tasks`, `findings` tables are the source of truth for run state.
- `.rvh-runs/cpg/<lang>.bin` + `.rvh-runs/tasks/<task>/cpg-mcp.log` — CPG artifact and each task's MCP tool-call trace; grepping `cpg-mcp.log` is the fastest way to see what the worker actually asked the CPG.

## Acknowledgments

- IU REALLMs cluster (RT team) for shared inference access.
- Anthropic's Mythos-preview red-team paper for the methodology that inspired this work.
