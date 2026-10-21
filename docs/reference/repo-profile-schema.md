# Repo Profile Schema

The repo profile is a JSON file in the target repository:

```text
.rvh-runs/repo_profile.json
```

rvh writes it during bootstrap. `init_run` writes an empty stub; after the repo-layout analysis writes `repo_analysis.json`, `scripts/derive_repo_profile.py` fills the profile in from that analysis and the file inventory. Deriving it makes no LLM call.

## Fields

```json
{
  "schema_version": 1,
  "source": "derived",
  "languages": ["css", "html", "javascript", "python"],
  "framework_hints": ["django", "docker", "python"],
  "entrypoint_paths": ["**/admin.py", "**/api/**/*.py", "**/urls.py", "**/views.py"],
  "hot_path_prefixes": ["api/", "gui/"],
  "hot_paths": [],
  "cold_path_prefixes": [".github/", "docs/", "tests/"],
  "cold_paths": [],
  "keyword_boosts": {},
  "notes": "Derived from repo_analysis.json + file inventory.",
  "created_at": "2026-10-20T14:02:11+00:00",
  "updated_at": "2026-10-20T14:03:40+00:00"
}
```

| Field | Derived from | Read by |
|---|---|---|
| `cold_path_prefixes` | The analysis's test, CI, docs, tooling, and generated paths, as `dir/` prefixes, minus any also listed as production | The scorer: files under these prefixes score 1 without an LLM call (see `scoring-rubric.md`) |
| `framework_hints` | Marker files (`manage.py`, `go.mod`, `Gemfile`, `next.config.js`, ...) and dependency-file matches (`flask` in `requirements.txt`, `"express"` in `package.json`, `gin-gonic/gin` in `go.mod`, ...) at the repo root and in production paths | The engine, for every worker task: each hint with a matching `prompts/worker_<lang>_<hint>.md` adds that framework snippet to the worker's language module; hints with no snippet are ignored |
| `languages` | The six most common languages among scanned files, by extension (stored sorted, like every list field) | Nothing; recorded for reference |
| `entrypoint_paths` | Entrypoint globs for detected frameworks (e.g. `**/views.py` for Django) | Nothing |
| `hot_path_prefixes` | The analysis's production paths, as `dir/` prefixes | Nothing |
| `hot_paths`, `cold_paths`, `keyword_boosts` | Always empty | Nothing |
| `schema_version`, `source`, `notes`, `created_at`, `updated_at` | Bookkeeping | Nothing |

The layout analysis also passes `framework_hints` into its own prompt, but it runs before the profile is derived, so on a normal run it sees the stub's empty list.

Framework snippets exist for Django, Flask, FastAPI, Rails, Sinatra, Laravel, Symfony, Express, NestJS, Next.js, Spring, Gin, and Axum.

## Editing the profile

The profile is derived once per run. Resuming doesn't re-derive it or re-score, so hand edits to `.rvh-runs/repo_profile.json` persist across resumes, but only `framework_hints` still has an effect at that point: the engine re-reads it for each task, so adding a framework the detection missed applies to every task dispatched after you resume. `cold_path_prefixes` is read only at scoring time, which a resumed run has already finished.

A profile with unknown keys or wrong value types stops the run with an error. `--fresh` deletes `.rvh-runs/`, profile included.
