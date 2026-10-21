"""Worker trace parsing (scripts/eval_trace.py)."""
import json

from eval_trace import parse_goose_trace, task_trace, tool_kind


def _call(name):
    return {"type": "toolRequest", "id": "1", "toolCall": {"status": "success", "value": {"name": name, "arguments": {}}}}


def _bad_call():
    return {"type": "toolRequest", "id": "2",
            "toolCall": {"status": "error", "error": "-32600: Tool 'read_file' was not advertised for this model turn"}}


def _result(text):
    return {"type": "toolResponse", "id": "1",
            "toolResult": {"status": "success", "value": {"content": [{"type": "text", "text": text}]}}}


def _error(text):
    return {"type": "toolResponse", "id": "2", "toolResult": {"status": "error", "error": text}}


SESSION = {
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "review app.py"}]},
        # goose 1.52: an unoffered tool fails at the request and again at the response.
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"}, _bad_call()]},
        {"role": "user", "content": [_error("-32600: Tool 'read_file' was not advertised for this model turn")]},
        {"role": "assistant", "content": [_call("shell")]},
        {"role": "user", "content": [_result("     3\tresult = eval(flask.request.args['q'])")]},
        {"role": "assistant", "content": [_call("python3__xref"), _call("todo__todo_write")]},
        {"role": "user", "content": [_result("[]"), _result("ok")]},
        # Older goose: the request "succeeds" and the response says the tool doesn't exist.
        {"role": "assistant", "content": [_call("todo_todoWrite")]},
        {"role": "user", "content": [_error("-32002: Tool 'todo_todoWrite' not found. Available tools: [shell]")]},
        {"role": "assistant", "content": [_call("recipe__final_output")]},
    ],
    "metadata": {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050, "status": "completed"},
}
BANNER = "    __( O)>  ● new session\n   \\____)    goose is ready\n"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_tool_kinds():
    assert [tool_kind(n) for n in ("shell", "python3__xref", "go__paths", "todo__todo_write",
                                   "recipe__final_output", "tree", "python3__unknown")] == \
        ["shell", "cpg", "cpg", "todo", "final", "other", "other"]


def test_a_session_is_summarized(tmp_path):
    stats = parse_goose_trace(_write(tmp_path / "stdout.txt", BANNER + json.dumps(SESSION)))
    assert stats.parsed
    assert stats.turns == 5
    assert dict(stats.tool_calls) == {"shell": 1, "cpg": 1, "todo": 1, "final": 1, "other": 1}
    assert stats.tool_errors == 1
    assert (stats.input_tokens, stats.output_tokens) == (1000, 50)
    assert stats.saw("result = eval(flask.request.args['q'])")
    assert not stats.saw("os.system(")


def test_calls_to_tools_the_harness_never_offered_are_named(tmp_path):
    """Trained-harness priors (read_file, TodoWrite) show up as unoffered tool calls."""
    stats = parse_goose_trace(_write(tmp_path / "stdout.txt", BANNER + json.dumps(SESSION)))
    assert dict(stats.unadvertised) == {"read_file": 1, "todo_todoWrite": 1}


def test_a_truncated_or_missing_trace_is_unparsed(tmp_path):
    text = BANNER + json.dumps(SESSION)
    assert not parse_goose_trace(_write(tmp_path / "cut.txt", text[: len(text) // 2])).parsed
    assert not parse_goose_trace(tmp_path / "absent.txt").parsed


def test_a_task_combines_its_attempts_and_only_its_own(tmp_path):
    run_dir = tmp_path / "run"
    for name in ("auto-0001-aaaa-task-7", "auto-0005-bbbb-task-7", "auto-0002-cccc-task-70"):
        _write(run_dir / "tasks" / name / "stdout.txt", BANNER + json.dumps(SESSION))
    stats = task_trace(run_dir, 7)
    assert stats.turns == 10
    assert stats.tool_calls["shell"] == 2
    assert stats.input_tokens == 2000
    assert stats.unadvertised["read_file"] == 2
