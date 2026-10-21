"""Tests for JSON → tab-separated text formatters."""
import pytest

import cpg_formatters as f


def test_format_paths_shows_hops():
    paths = [
        {"nodes": [
            {"file": "a.cpp", "line": 5, "code": "input = fgets(...)"},
            {"file": "a.cpp", "line": 12, "code": "url = parse(input)"},
            {"file": "b.cpp", "line": 20, "code": "curl_easy_setopt(h, URL, url)"},
        ]},
    ]
    out = f.format_paths(paths)
    assert "a.cpp:5" in out
    assert "a.cpp:12" in out
    assert "b.cpp:20" in out
    # Path numbering (1 of 1)
    assert "Path 1" in out or "path 1" in out.lower()


def test_format_func_single():
    row = {"name": "Foo::bar", "file": "x.cpp", "line": 7, "signature": "int bar(int)", "numCallees": 3, "numParameters": 1}
    out = f.format_func([row])
    assert "Foo::bar" in out
    assert "x.cpp:7" in out
    assert "numCallees" in out or "callees" in out.lower()


def test_format_callers_tree():
    rows = [
        {"file": "a.cpp", "line": 5, "name": "main"},
        {"file": "b.cpp", "line": 10, "name": "parse_config"},
    ]
    out = f.format_callers(rows)
    assert "a.cpp:5" in out
    assert "b.cpp:10" in out


def test_format_error_returns_candidate_list():
    out = f.format_ambiguous_symbol(
        symbol="bar", candidates=["Foo::bar(int)", "Baz::bar()"]
    )
    assert "Foo::bar" in out
    assert "Baz::bar" in out
    assert "ambiguous" in out.lower() or "multiple" in out.lower()
