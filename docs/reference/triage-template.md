# Triage Template

A checklist for a reviewer deciding what to do with one candidate from a worker report (`.rvh-runs/reports/task-N.md`, indexed in `.rvh-runs/CANDIDATES.md`). For a full precision pass over a completed scan that ends in a maintainer report, use `codex-triage-report-prompt.md` instead.

rvh doesn't record triage. Every finding it stores stays a candidate; keep your verdicts wherever you track review.

## Goal

Decide whether a candidate is:

- real and well-supported
- interesting enough to surface to maintainers
- a duplicate of another candidate
- too weak or speculative to carry

## Questions

1. Does the report identify a concrete code path and failure mode?
2. Is attacker control or trust-boundary crossing explained clearly?
3. Does the impact description match the evidence?
4. Is the issue duplicative of another report?
5. Is this actually a context note, UI-only clue, or remediation note rather than a distinct vulnerability?
6. Is there enough detail for a maintainer to verify and fix it?

## Output

Return:

- verdict: `confirmed`, `duplicate`, or `rejected`
- one-paragraph rationale
- any follow-up checks worth doing before surfacing it

Triage heuristics:

- mark `confirmed` when you have traced the source-to-sink path in the current code and it holds
- mark `duplicate` when the candidate rediscovers an already-confirmed route, trust boundary, or root cause
- mark `rejected` when the report is only UI gating, navigation, missing tests, or other context without an independent vulnerability
