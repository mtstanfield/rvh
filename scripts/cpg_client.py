"""HTTP client for joern --server.

Joern exposes a CPGQL REPL at ``POST /query-sync``. The response body is JSON
with fields ``success: bool``, ``stdout: str`` (the full Scala REPL transcript
for the submitted query), ``stderr: str``, and ``uuid: str``.

The REPL's ``stdout`` contains *value bindings* — one ``val resN: Type = value``
line per value-producing expression. ``println(...)`` output is not captured
here, so we cannot use printed markers to frame our payload. Instead we send
bare ``<expression>.toJsonPretty`` and parse the *last* ``val resN: String =``
line out of stdout, Scala-unescape the literal, and ``json.loads`` it.

The REPL prints strings in two forms:

  * Double-quoted (``val r: String = "..."``) with C-style backslash escapes
    when the content is simple enough.
  * Triple-quoted (``val r: String = \"\"\"...\"\"\"``) raw form (no escapes,
    literal newlines allowed) when the content contains quotes or newlines.

ANSI color codes may appear anywhere in stdout — we strip those first.
"""
from __future__ import annotations

import codecs
import json
import re
from typing import Any

import requests

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Triple-quoted form: `val resN: String = """...any chars..."""` — non-greedy
# body, DOTALL so newlines inside the body match.
_TRIPLE_RE = re.compile(
    r'val\s+res\d+\s*:\s*String\s*=\s*"""(.*?)"""',
    re.DOTALL,
)

# Double-quoted form: `val resN: String = "..."` — body is any chars that
# aren't an unescaped double-quote. Must run after triple-quote extraction.
_DOUBLE_RE = re.compile(
    r'val\s+res\d+\s*:\s*String\s*=\s*"((?:[^"\\]|\\.)*)"',
)


class CpgQueryError(RuntimeError):
    """Raised when a CPG query fails to execute or returns an error."""


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _scala_unescape_double(literal: str) -> str:
    """Decode Scala double-quoted string escapes (\\n, \\t, \\\", \\\\, etc.).

    Scala's double-quoted escapes overlap with Python's ``unicode_escape``
    codec for the cases we care about: ``\\"``, ``\\\\``, ``\\n``, ``\\t``,
    ``\\r``, ``\\b``, ``\\f``, and ``\\uHHHH``. ``codecs.decode`` handles
    them cleanly.
    """
    return codecs.decode(literal, "unicode_escape")


def _find_last_string_binding(cleaned_stdout: str) -> str | None:
    """Return the value of the last ``val resN: String = ...`` line, or None.

    Scans both triple- and double-quoted forms, picks whichever ends latest
    in the transcript.
    """
    candidates: list[tuple[int, str]] = []
    for m in _TRIPLE_RE.finditer(cleaned_stdout):
        # Triple-quoted body is raw — no escape processing.
        candidates.append((m.end(), m.group(1)))
    for m in _DOUBLE_RE.finditer(cleaned_stdout):
        candidates.append((m.end(), _scala_unescape_double(m.group(1))))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


class CpgClient:
    def __init__(self, endpoint: str, timeout: float = 120.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def query(self, scala: str) -> Any:
        """POST a Scala expression and return the parsed JSON result.

        Raises CpgQueryError on HTTP failure, Joern compilation failure, or
        malformed response.
        """
        try:
            r = requests.post(
                f"{self.endpoint}/query-sync",
                json={"query": scala},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise CpgQueryError(f"HTTP error: {e}") from e
        if r.status_code != 200:
            raise CpgQueryError(f"HTTP {r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except ValueError as e:
            raise CpgQueryError(f"non-JSON response: {r.text[:500]}") from e
        stdout = _strip_ansi(data.get("stdout", ""))
        stderr = data.get("stderr", "") or ""
        if not data.get("success"):
            # Fall back to stdout tail if stderr is empty — Joern sometimes
            # reports compile errors inline rather than on stderr.
            msg = stderr.strip() or stdout.strip()[-500:]
            raise CpgQueryError(f"joern error: {msg[:500]}")
        payload = _find_last_string_binding(stdout)
        if payload is None:
            # Query didn't produce a `val resN: String = ...` line. Usually
            # this means a compile error rendered in stdout itself, or the
            # expression returned Unit / a non-String type.
            tail = stdout.strip()[-500:]
            raise CpgQueryError(
                f"no String result in stdout (compile error?): {tail}"
            )
        payload = payload.strip()
        if not payload:
            return []
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise CpgQueryError(
                f"malformed JSON in String result: {payload[:500]}"
            ) from e
