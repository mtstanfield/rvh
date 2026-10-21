"""Cost and tool usage from a worker's session trace.

Only goose's ``--output-format json`` trace (``stdout.txt`` in each task dir)
is parsed today; another agent harness adds its own ``parse_*_trace``
returning the same ``TraceStats``.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CPG_TOOLS = frozenset({"paths", "reachable", "callers", "callees", "xref", "func", "inspect", "entrypoints"})
# goose 1.52: "Tool 'x' was not advertised for this model turn"; older: "Tool 'x' not found".
_UNOFFERED = re.compile(r"Tool '([^']+)' (?:was not advertised|not found)")


@dataclass
class TraceStats:
    parsed: bool = False
    turns: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    # Calls to tools the harness never offered — usually the model reaching for
    # a tool from the harness it was trained against (read_file, TodoWrite).
    unadvertised: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    outputs: list[str] = field(default_factory=list)  # every tool result's text

    def add(self, other: TraceStats) -> None:
        """Fold another attempt of the same task into this one."""
        self.parsed = self.parsed or other.parsed
        self.turns += other.turns
        self.tool_calls.update(other.tool_calls)
        self.tool_errors += other.tool_errors
        self.unadvertised.update(other.unadvertised)
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.outputs.extend(other.outputs)

    def saw(self, needle: str) -> bool:
        """Whether ``needle`` appeared in any tool output."""
        return any(needle in text for text in self.outputs)


def tool_kind(name: str) -> str:
    """shell | cpg | todo | final | other."""
    if name == "shell":
        return "shell"
    if name == "recipe__final_output":
        return "final"
    prefix, _, tool = name.partition("__")
    if prefix == "todo":
        return "todo"
    if tool in CPG_TOOLS:
        return "cpg"
    return "other"


def parse_goose_trace(path: Path) -> TraceStats:
    """Stats for one goose session; ``parsed`` is False when the trace is missing or truncated."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw[raw.index("{"):])
    except (OSError, ValueError):
        return TraceStats()
    stats = TraceStats(parsed=True)
    for message in data.get("messages") or []:
        if message.get("role") == "assistant":
            stats.turns += 1
        for block in message.get("content") or []:
            if block.get("type") == "toolRequest":
                call = block.get("toolCall") or {}
                if call.get("status") != "success":
                    stats.tool_errors += 1
                    continue
                stats.tool_calls[tool_kind((call.get("value") or {}).get("name", ""))] += 1
            elif block.get("type") == "toolResponse":
                result = block.get("toolResult") or {}
                # Both goose versions report an unoffered tool on the response,
                # so counting here counts each call once.
                unoffered = _UNOFFERED.search(str(result.get("error") or ""))
                if unoffered:
                    stats.unadvertised[unoffered.group(1)] += 1
                for item in (result.get("value") or {}).get("content") or []:
                    if item.get("type") == "text":
                        stats.outputs.append(item.get("text") or "")
    meta = data.get("metadata") or {}
    stats.input_tokens = int(meta.get("input_tokens") or 0)
    stats.output_tokens = int(meta.get("output_tokens") or 0)
    return stats


def task_trace(run_dir: Path, task_id: int) -> TraceStats:
    """Every attempt of one task, combined (each attempt has its own task dir)."""
    total = TraceStats()
    for task_dir in sorted((run_dir / "tasks").glob(f"*-task-{task_id}")):
        total.add(parse_goose_trace(task_dir / "stdout.txt"))
    return total
