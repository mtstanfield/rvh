"""Tests for cpg_client.py — HTTP wrapper around joern --server.

The REPL's ``stdout`` is a value-binding transcript. We parse the last
``val resN: String = ...`` line, unescape (if double-quoted), then
``json.loads`` the content.
"""
import pytest
import requests_mock

import cpg_client


def _triple_quoted_stdout(json_payload: str, *, res_index: int = 1) -> str:
    """Real joern emits triple-quoted form for multi-line or quote-bearing JSON."""
    return (
        "val res0: Option[Cpg] = Some(value = Cpg[Graph[36 nodes]])\n"
        f'val res{res_index}: String = """{json_payload}"""\n'
    )


def _double_quoted_stdout(scala_escaped: str, *, res_index: int = 1) -> str:
    """Scala-escaped double-quoted form — used for short payloads without newlines."""
    return (
        "val res0: Option[Cpg] = Some(value = Cpg[Graph[36 nodes]])\n"
        f'val res{res_index}: String = "{scala_escaped}"\n'
    )


def test_query_returns_parsed_json_triple_quoted():
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": _triple_quoted_stdout('[{"file":"a.cpp","line":5}]'),
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        result = c.query("cpg.method.l")
    assert result == [{"file": "a.cpp", "line": 5}]


def test_query_returns_parsed_json_double_quoted():
    # Double-quoted form: joern escapes internal `"` as `\"`.
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": _double_quoted_stdout(r'[\"main\", \"handle\"]'),
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        result = c.query("cpg.method.name.toJsonPretty")
    assert result == ["main", "handle"]


def test_query_picks_last_string_binding():
    """Earlier res bindings (Option[Cpg], etc.) must be ignored.

    Only the final String binding is our payload.
    """
    stdout = (
        'val res0: Option[Cpg] = Some(value = Cpg[Graph[36 nodes]])\n'
        'val res1: String = "not-the-answer"\n'
        'val res2: String = """[{"file":"a.cpp"}]"""\n'
    )
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={"success": True, "stdout": stdout, "stderr": ""},
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        result = c.query("cpg.method.l")
    assert result == [{"file": "a.cpp"}]


def test_query_strips_ansi_color_codes():
    with requests_mock.Mocker() as m:
        ansi_wrapped = (
            "\x1b[33mval\x1b[0m res0 = 42\n"
            '\x1b[36mval\x1b[0m res1: String = """[]"""\n'
        )
        m.post(
            "http://localhost:8080/query-sync",
            json={"success": True, "stdout": ansi_wrapped, "stderr": ""},
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        assert c.query("cpg.method.l") == []


def test_query_empty_result_returns_empty_list():
    """Triple-quoted empty JSON array should parse to []."""
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": _triple_quoted_stdout("[]"),
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        assert c.query("cpg.method.l") == []


def test_query_raises_on_server_error():
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": False,
                "stdout": "",
                "stderr": "compile error: unknown symbol Foo",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        with pytest.raises(cpg_client.CpgQueryError) as exc:
            c.query("bad.scala")
    assert "compile error" in str(exc.value)


def test_query_raises_on_http_error():
    with requests_mock.Mocker() as m:
        m.post("http://localhost:8080/query-sync", status_code=500, text="server down")
        c = cpg_client.CpgClient("http://localhost:8080")
        with pytest.raises(cpg_client.CpgQueryError):
            c.query("cpg.method.l")


def test_query_raises_when_no_string_binding():
    """If the query didn't return a String (e.g. compile error printed inline,
    or the expression returned Unit), stdout has no ``val resN: String = ...``.
    """
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": "val res0: Int = 42\n",  # no String binding
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        with pytest.raises(cpg_client.CpgQueryError) as exc:
            c.query("cpg.method.l")
    assert "no String result" in str(exc.value) or "compile" in str(exc.value).lower()


def test_query_raises_on_malformed_json_in_string():
    """A String binding that isn't valid JSON should raise."""
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": _triple_quoted_stdout("not-json-at-all"),
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080")
        with pytest.raises(cpg_client.CpgQueryError) as exc:
            c.query("cpg.method.l")
    assert "malformed" in str(exc.value).lower() or "JSON" in str(exc.value)


def test_query_has_configurable_timeout():
    with requests_mock.Mocker() as m:
        m.post(
            "http://localhost:8080/query-sync",
            json={
                "success": True,
                "stdout": _triple_quoted_stdout("[]"),
                "stderr": "",
            },
        )
        c = cpg_client.CpgClient("http://localhost:8080", timeout=5.0)
        c.query("cpg.method.l")
    assert m.call_count == 1
