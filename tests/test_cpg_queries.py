"""Tests for Scala query string construction."""
import pytest

import cpg_queries as q


def test_entrypoints_query_defaults_safe():
    query = q.build_entrypoints()
    assert "cpg.method" in query
    assert (
        "internal" in query
        or "isExternal" in query
        or "isExported" in query
        or "main" in query
    )
    # Default limit must be applied so a bare call can't blow context
    assert ".take(200)" in query
    # Synthetic methods must be filtered — otherwise Python repos
    # return `<module>` + `<body>` + `<fakeNew>` by the thousand
    assert "<module>" in query
    assert "<fakeNew>" in query
    assert "<metaClassCallHandler>" in query
    assert ".toJsonPretty" in query


def test_entrypoints_query_file_filter():
    query = q.build_entrypoints(file="gui/views.py", limit=50)
    assert "gui/views.py" in query
    assert ".take(50)" in query


def test_xref_query_applies_limit():
    query = q.build_xref(symbol="get", limit=42)
    assert ".take(42)" in query


def test_callers_query_applies_limit():
    query = q.build_callers(symbol="foo", depth=3, limit=25)
    assert ".take(25)" in query


def test_callees_query_applies_limit():
    query = q.build_callees(symbol="foo", depth=3, limit=25)
    assert ".take(25)" in query


def test_func_query_embeds_name_safely():
    query = q.build_func(name="Validator::store_public_ec_key")
    # Must escape/quote the name — no raw interpolation that could break Scala
    assert "Validator::store_public_ec_key" in query
    # No shell metacharacters leaked
    assert ";" not in query.replace("toJson", "").replace("->", "")


def test_func_query_rejects_injection_attempts():
    with pytest.raises(ValueError):
        q.build_func(name='"); System.exit(1); val x = ("')


def test_paths_query_references_from_and_to():
    query = q.build_paths(src="func_a", dst="func_b", max_paths=10)
    assert "func_a" in query
    assert "func_b" in query
    assert "reachableBy" in query or "reachable" in query.lower()


def test_reachable_query_uses_default_source_pattern():
    """Without sources_hint, reachable seeds with the C-style default."""
    q1 = q.build_reachable(sink_file="src/foo.cpp", sink_line=42)
    assert "src/foo.cpp" in q1
    assert "42" in q1
    assert "argv" in q1  # default C-style seed must appear in the Scala


def test_reachable_query_honors_sources_hint():
    """Django-style sources_hint replaces the default seed pattern."""
    q1 = q.build_reachable(
        sink_file="api/views.py", sink_line=150,
        sources_hint=r"request\.GET|request\.POST",
    )
    assert "request" in q1 and "GET" in q1 and "POST" in q1
    assert "argv" not in q1  # default must NOT be present when hint is passed


def test_reachable_query_rejects_control_chars_in_hint():
    with pytest.raises(ValueError):
        q.build_reachable(
            sink_file="x.py", sink_line=1, sources_hint="bad\nregex",
        )


def test_callers_query_depth():
    q1 = q.build_callers(symbol="Foo::bar", depth=3)
    assert "Foo::bar" in q1
    # depth should manifest as a walk of length 3
    assert "repeat" in q1 or "3" in q1


def test_callees_query_depth():
    q1 = q.build_callees(symbol="Foo::bar", depth=2)
    assert "Foo::bar" in q1


def test_xref_query():
    q1 = q.build_xref(symbol="g_config")
    assert "g_config" in q1


def test_xref_orders_calls_before_identifiers():
    # Call nodes are sinks and must survive take(limit) ahead of the
    # identifier flood. Regression: an eval(...) call-site was hidden behind
    # dozens of module-level `eval` identifier hits at the default limit.
    query = q.build_xref(symbol="eval")
    assert query.index("cpg.call.name") < query.index("cpg.identifier.name")


def test_inspect_query():
    q1 = q.build_inspect(file="src/x.cpp", line=100)
    assert "src/x.cpp" in q1
    assert "100" in q1


