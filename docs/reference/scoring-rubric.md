# Scoring Rubric

rvh scores every in-scope file 1–5 for how much security review it warrants. Workers run only on files scored at or above `--min-priority` (default 3), highest first.

Files the inventory excludes are never scored: binaries, docs, images, lockfiles, files over 256 KiB, vendored code (unless `--include-vendored`), and anything outside `--include` or matched by `--exclude`.

## The 1–5 scale

The scale both LLM scoring prompts use (`prompts/score_phase1.md`, `prompts/score_phase2.md`):

- **5** — parses network or user-supplied input; implements authentication, authorization, or access control; performs cryptographic operations; builds shell commands, SQL queries, or file paths from external input.
- **4** — production code that handles trust-boundary data or sits one hop from an external entry point (e.g. an internal API a public handler calls).
- **3** — production logic with non-trivial data handling and no obvious external interface.
- **2** — utilities, internal helpers, glue code, configuration without external input.
- **1** — constants, data files, generated schemas, fixtures: no executable logic.

Test code, CI configuration, documentation, developer tooling, and generated code score 1–2.

## How a file gets its score

Scoring runs once per run, during bootstrap, right after the repo-layout analysis (`scripts/score_files.py`). Each file takes the first rule that applies; the rule is stored as `scored_by`.

1. **Prefilter → 1** (`prefilter`), no LLM call, when:
   - the path starts with one of the repo profile's `cold_path_prefixes`: the test, CI, docs, tooling, and generated directories the layout analysis found (see `repo-profile-schema.md`);
   - the file isn't code and its extension isn't one the scorer reads as text, so `.html`, `.xml`, `.sql`, and similar files score 1;
   - it is binary, or a docs, asset, or metadata file (the inventory normally excludes these already).
2. **Oversized → 3** (`oversized-skip`): larger than 256,000 bytes. A backstop for generated blobs; the inventory already excludes files over 256 KiB.
3. **Pure config → 2** (`config-cap`): `.conf`, `.cfg`, and `.ini` files. The worker prompt is written for code, so these stay below the default `--min-priority`.
4. **Phase 1, path only.** The remaining paths go to the model in batches of 100, along with the repo layout from the analysis step. Paths the model drops are retried in batches of 25; any still missing get 3.
5. **Phase 2, content.** Files that scored 2 or higher in phase 1 are re-scored from a content sample, five files per call: the whole file if it is 4 KB or smaller, otherwise the first 4 KB plus 4 KB from the middle. The model moves the path-only score up or down based on what the code does.

A phase-2 score is final (`llm-content`), with one floor: if phase 1 gave a code file 3 or higher, phase 2 can't push it below 3. Files with no phase-2 score keep their phase-1 score (`llm-path`): files phase 1 scored 1, and files whose phase-2 batch failed after its retry.

## Where scores are stored

Each score lands in the `scores` table of `.rvh-runs/state.db` (`score`, `rationale`, `scored_by`, `signals_json`) and becomes the priority of that file's task. `signals_json` records which prefilter rule fired (e.g. `cold-prefix:tests/`), or the phase-1 score and whether the floor applied. `--debug` saves every scoring prompt and response under `.rvh-runs/logs/debug/`.

Scores are computed once. Resuming a run keeps them; `--fresh` starts over and re-scores.
