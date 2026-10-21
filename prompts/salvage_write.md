# Salvage report write

A prior worker session analyzed `{anchor_path}` for security vulnerabilities. The investigation reached a conclusion but never reported it. Your job is to report the prior analysis with one `recipe__final_output(...)` call. **Do not re-investigate. Do not run shell commands to read source.** The analysis below is the finding-or-no-finding determination.

## Context

- Anchor: {anchor_path}
- Task ID: {task_id}

## Prior analysis (verbatim from the previous worker turn)

```
{prior_analysis}
```

## What to do

1. Decide whether the prior analysis describes a real vulnerability with a concrete sink and trust boundary, or whether it found nothing actionable. If the analysis enumerates issues but concludes they are low-risk hardening / not exploitable / internal-only, that is `no-finding`. If it identifies a specific sink reachable from attacker-controlled input with insufficient defenses, that is `finding`.

2. Call `recipe__final_output` exactly once:

   - `verdict`: `"finding"` or `"no-finding"`
   - `anchor_file`: `"{anchor_path}"`
   - `task_id`: `{task_id}`
   - `summary`: the primary deliverable. For `finding`, one block per candidate the prior analysis confirmed:

         - <title> — <severity> · CWE-<digits>
           - location: file:line
           - source → sink: <attacker input → … → sink>
           - why insufficient: <why the present defenses don't stop it>

     For `no-finding`: what was investigated and which defense eliminated each hypothesis.
   - `findings`: best-effort structured metadata for the headline finding (`[]` for no-finding). Every candidate must appear in the `summary` whether or not it is in `findings`.

## Field constraints

- `severity`: exactly one of `"info"`, `"low"`, `"medium"`, `"high"`, `"critical"`. Never blend (`"medium-high"`) or abbreviate (`"med"`).
- `cwe`: exactly `"CWE-<digits>"`. If the prior analysis didn't name a CWE for a finding, pick the closest standard CWE based on the bug class (e.g. CWE-78 for shell injection, CWE-89 for SQL injection, CWE-22 for path traversal, CWE-95 for eval, CWE-287 for missing auth).
- `sink_category`: one of `shell`, `sql`, `fs`, `url`, `deserialization`, `template`, `authorization`, `crypto`, `logging`.
- Goose validates the call against the schema; a rejected call gives you an actionable error to fix and retry.

If `recipe__final_output` is not registered, write the report as markdown with YAML frontmatter (`verdict`, `finding_type`, `cwe`, `severity`, `title`, `anchor_file`, `task_id`) followed by `# Summary`, to `{report_path}` with `write`.

You are running autonomously with no user available. Do not ask questions. Your only deliverable is the `recipe__final_output` call.
