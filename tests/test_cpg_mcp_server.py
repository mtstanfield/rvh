"""Tests for scripts/cpg_mcp_server.py — FastMCP server exposing rvh-cpg
operations as distinct tools."""
from unittest.mock import MagicMock, patch

import pytest

import cpg_mcp_server


@pytest.fixture(autouse=True)
def _reset_call_cache():
    """Clear the per-session dedup cache before every test so cache
    state from an earlier test case can't silently short-circuit the
    sidecar call path in a later test."""
    cpg_mcp_server._clear_call_cache()
    yield
    cpg_mcp_server._clear_call_cache()


EXPECTED_TOOL_NAMES = {
    "paths", "reachable",
    "callers", "callees", "xref", "func", "inspect", "entrypoints",
}


def test_eight_tools_registered():
    """The MCP server must register exactly the 8 expected tools."""
    names = set(cpg_mcp_server.registered_tool_names())
    assert names == EXPECTED_TOOL_NAMES, (
        f"expected {EXPECTED_TOOL_NAMES}, got {names}"
    )


def test_reachable_passes_sources_hint_to_query_builder():
    """The `sources_hint` parameter must propagate into the Scala query."""
    from unittest.mock import MagicMock, patch
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        cpg_mcp_server._tool_reachable(
            "api/views.py:42",
            sources_hint=r"request\.GET|request\.POST",
        )
    scala = fake_client.query.call_args.args[0]
    assert "request" in scala and "GET" in scala and "POST" in scala


def test_reachable_without_sources_hint_uses_default():
    """Omitting sources_hint must fall through to the C-style default."""
    from unittest.mock import MagicMock, patch
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        cpg_mcp_server._tool_reachable("src/foo.cpp:199")
    scala = fake_client.query.call_args.args[0]
    assert "argv" in scala


def test_cap_output_leaves_small_results_alone():
    """Outputs under the cap must pass through byte-identical."""
    small = "line1\nline2\nline3\n"
    assert cpg_mcp_server._cap_output(small) == small


def test_cap_output_truncates_oversized_with_note():
    """Outputs over MAX_TOOL_OUTPUT_BYTES must be truncated and tagged."""
    over = "x" * (cpg_mcp_server.MAX_TOOL_OUTPUT_BYTES + 5000)
    result = cpg_mcp_server._cap_output(over)
    assert len(result) < len(over)
    assert "truncated" in result.lower()
    assert "more bytes" in result.lower()
    # Head must still carry the beginning of the data
    assert result.startswith("xxxxx")


def test_cap_output_handles_non_string_gracefully():
    """Anything that isn't a str must pass through untouched."""
    assert cpg_mcp_server._cap_output(None) is None
    assert cpg_mcp_server._cap_output(["rows"]) == ["rows"]


def test_dedup_cache_returns_cached_on_identical_call():
    """Calling a tool with the same args twice returns cached result
    with a 'duplicate call' note — the sidecar is NOT hit the second
    time."""
    cpg_mcp_server._clear_call_cache()
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        r1 = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
        # Second identical call must not touch client.query again
        query_calls_after_first = fake_client.query.call_count
        r2 = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
    assert fake_client.query.call_count == query_calls_after_first, (
        "second identical call hit the sidecar; dedup not applied"
    )
    assert "duplicate call" in r2.lower()
    # The cached body (after the note) matches the first result content
    assert "no reaching source" in r2


def test_dedup_cache_distinguishes_by_args():
    """Different args → separate cache entries, both hit the sidecar."""
    cpg_mcp_server._clear_call_cache()
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        cpg_mcp_server._tool_reachable("src/foo.cpp:199")
        cpg_mcp_server._tool_reachable("src/bar.cpp:42")
    # Two distinct args = two sidecar calls
    assert fake_client.query.call_count == 2


def test_dedup_cache_distinguishes_by_tool_name():
    """Same args passed to different tools must not collide — dedup keys
    include the tool name."""
    cpg_mcp_server._clear_call_cache()
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        cpg_mcp_server._tool_xref("foo")  # xref's cached
        # callers("foo", 3, 100) shouldn't pick up xref's cached entry
        cpg_mcp_server._tool_callers("foo", 3, 100)
    assert fake_client.query.call_count == 2


def test_dedup_count_increments_on_repeated_hits():
    """Third duplicate shows #3, etc. — the note tracks repetition."""
    cpg_mcp_server._clear_call_cache()
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        cpg_mcp_server._tool_reachable("src/foo.cpp:199")  # fresh
        r2 = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
        r3 = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
    assert "duplicate call #2" in r2
    assert "duplicate call #3" in r3


def test_make_client_errors_when_endpoint_unset(monkeypatch):
    """Missing RVH_CPG_ENDPOINT must raise a clear RuntimeError."""
    monkeypatch.delenv("RVH_CPG_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError) as exc:
        cpg_mcp_server._make_client()
    assert "RVH_CPG_ENDPOINT" in str(exc.value)


def test_empty_reachable_result_includes_library_code_hint():
    """Empty reachable() must append a next-step hint pointing at callers()
    so the worker doesn't misread 'no reaching source' as 'safe' on
    library code called from outside the CPG's source model."""
    fake_client = MagicMock()
    fake_client.query.return_value = []  # empty rows
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
    assert "no reaching source" in result
    # Hint must mention the library-code caveat + suggest callers()
    assert "callers(" in result
    assert "library code" in result.lower() or "C-API" in result


def test_empty_paths_result_includes_callers_hint():
    """Empty paths() must suggest callers() on the sink symbol."""
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_paths(
            src="picojson::parse", dst="curl_easy_setopt",
        )
    assert 'callers(symbol="curl_easy_setopt")' in result


def test_empty_callers_result_includes_xref_hint():
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_callers(symbol="SimpleCurlGet::perform_start")
    assert 'xref(symbol="SimpleCurlGet::perform_start")' in result


def test_non_empty_reachable_result_suggests_paths():
    """Non-empty reachable should steer worker to paths() for the full
    hop chain — not the library-code hint."""
    fake_client = MagicMock()
    fake_client.query.return_value = [
        {"source_file": "src/main.c", "source_line": 10,
         "hops": 3, "source_code": "argv[1]"},
    ]
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_reachable("src/foo.cpp:199")
    assert "no reaching source" not in result
    assert "library code" not in result.lower()
    # New: non-empty hint surfaces paths() as the next call
    assert "paths(" in result
    assert "Next step:" in result or "next step" in result.lower()


def test_non_empty_callers_result_suggests_paths():
    """Non-empty callers() must steer worker toward paths(src=..., dst=symbol)
    so the multi-hop flow finally gets traced."""
    fake_client = MagicMock()
    fake_client.query.return_value = [
        {"file": "src/foo.cpp", "line": 252, "name": "get_metadata"},
        {"file": "src/foo.cpp", "line": 910, "name": "get_public_keys_from_web"},
    ]
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_callers(symbol="SimpleCurlGet::perform_start")
    # Non-empty hint names paths() with the target symbol pre-filled as dst.
    assert 'paths(src=' in result
    assert 'dst="SimpleCurlGet::perform_start"' in result
    # And suggests common src candidates (picojson::parse is the one that
    # exercises the canonical multi-hop-SSRF in scitokens-cpp).
    assert "picojson::parse" in result


def test_empty_xref_result_steers_to_a_text_search():
    """An empty xref must not read as "the symbol is unused": the CPG can miss
    real calls (joern's Go frontend drops calls inside closures), so the hint
    points at a text search on the short name — and never at tools the
    server doesn't have."""
    fake_client = MagicMock()
    fake_client.query.return_value = []
    with patch("cpg_mcp_server._make_client", return_value=fake_client):
        result = cpg_mcp_server._tool_xref("director.LaunchPeriodicDirectorTest")
    assert "rg -n 'LaunchPeriodicDirectorTest'" in result
    assert "sinks" not in result and "sources" not in result
