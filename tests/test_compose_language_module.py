"""Tests for worker.py:_compose_language_module — verifies framework-snippet
loading honors the framework_hints argument and degrades gracefully when
no matching snippet exists.

This is the binding test for the auth-gate-audit feature: framework_hints
flows from repo_profile.json → run_autonomous_v2 → render_worker_prompt →
_compose_language_module, where matching `worker_<lang>_<fw>.md` files get
appended to the language module.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from worker import _compose_language_module


def test_compose_loads_python_django_snippet_when_hint_present():
    """The Django framework snippet appends to the Python language module
    when 'django' is in framework_hints. Anchor extension drives `lang=python`."""
    out = _compose_language_module(
        "src/views.py", framework_hints=("django",)
    )
    assert "Python-specific review notes" in out  # base module loaded
    assert "Detected framework: Django" in out    # snippet appended


def test_compose_omits_framework_snippet_when_hint_absent():
    """No django hint → no Django snippet, even on a .py anchor."""
    out = _compose_language_module(
        "src/views.py", framework_hints=()
    )
    assert "Python-specific review notes" in out
    assert "Detected framework: Django" not in out


def test_compose_loads_multiple_framework_snippets():
    """Multiple framework hints → each matching snippet appended."""
    out = _compose_language_module(
        "src/api.py",
        framework_hints=("flask", "fastapi"),
    )
    assert "Detected framework: Flask" in out
    assert "Detected framework: FastAPI" in out


def test_compose_skips_unknown_framework_hint_silently():
    """A hint with no matching `worker_<lang>_<fw>.md` is silently skipped —
    the per-language module still loads, and known hints still attach."""
    out = _compose_language_module(
        "src/api.py",
        framework_hints=("django", "totally_made_up_framework"),
    )
    assert "Detected framework: Django" in out
    assert "totally_made_up_framework" not in out


def test_compose_returns_empty_for_unknown_extension():
    """Anchors with unknown extensions still return empty — the framework
    hint is ignored because there's no language module to attach to."""
    out = _compose_language_module(
        "src/x.zig", framework_hints=("django",)
    )
    assert out == ""


def test_compose_ignores_invalid_hint_entries():
    """Empty strings, None, and non-strings are skipped without raising."""
    out = _compose_language_module(
        "src/x.py",
        framework_hints=("", None, 42, "django"),  # type: ignore[arg-type]
    )
    assert "Detected framework: Django" in out


def test_compose_loads_js_express_snippet():
    """Sanity check coverage for a non-Python framework: JS + express hint
    loads the express snippet."""
    out = _compose_language_module(
        "routes/api.js", framework_hints=("express",)
    )
    assert "JavaScript-specific review notes" in out
    assert "Detected framework: Express" in out


def test_compose_loads_ts_nestjs_snippet():
    """TS + nestjs hint → TS module + NestJS snippet."""
    out = _compose_language_module(
        "src/users.controller.ts",
        framework_hints=("nestjs",),
    )
    assert "TypeScript-specific review notes" in out
    assert "Detected framework: NestJS" in out


def test_compose_loads_rust_axum_snippet():
    out = _compose_language_module(
        "src/main.rs", framework_hints=("axum",)
    )
    # rust language module's own header differs from the others; check by
    # the framework snippet only, plus a sentinel from the rust module.
    assert "Detected framework: Axum" in out
    assert "Rust" in out


def test_compose_loads_php_laravel_snippet():
    out = _compose_language_module(
        "app/Http/Controllers/UserController.php", framework_hints=("laravel",),
    )
    assert "PHP-specific review notes" in out
    assert "Detected framework: Laravel" in out


def test_compose_authorization_gates_section_in_each_language_module():
    """Every web-framework-relevant language module carries an Authorization
    gates section — covers undetected frameworks via the abstract pattern."""
    web_langs = [
        "foo.py", "foo.js", "foo.ts", "Foo.java",
        "foo.go", "foo.rs", "foo.rb", "foo.php",
    ]
    for path in web_langs:
        out = _compose_language_module(
            path, framework_hints=()
        )
        assert "Authorization gates" in out, (
            f"{path}: missing 'Authorization gates' section in language module"
        )
