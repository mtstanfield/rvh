"""Every file type that holds reviewable source code reaches a worker.

Four tables decide that: inventory_repo.CODE_EXTENSIONS (is it code?), the
scorer's prefilter (does the model score it?), worker._EXT_TO_LANG (which
language module does the worker get?) and cpg_build._EXT_TO_CPG_LANG (does
it get CPG tools?). They drifted apart: Vue components, CakePHP .ctp views
and Perl .pm modules were all scored 1 by the prefilter and never scanned.
"""
from __future__ import annotations

import pytest

import cpg_build
import inventory_repo as inv
import score_files
from worker import _EXT_TO_LANG, _compose_language_module

# Files whose code is a language embedded in another format: component and
# view files, and zeek's parser/builtin definitions (C++ inside %{ %}).
EMBEDDED_SOURCE = {
    ".vue": "JavaScript-specific review notes",
    ".svelte": "JavaScript-specific review notes",
    ".ctp": "PHP-specific review notes",
    ".phtml": "PHP-specific review notes",
    ".erb": "Ruby-specific review notes",
    ".pac": "C/C++-specific review",
    ".bif": "C/C++-specific review",
}
# Extensions the worker or CPG maps already knew that the inventory did not.
DRIFTED = [".pm", ".mjs", ".cjs", ".pyi", ".hxx", ".kts"]


def _categorize(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    category, _tags, _excluded, _reason = inv.categorize_file(
        f"src/{name}", p, p.suffix, False,
        vendor_policy="exclude", generated_policy="exclude", test_policy="include",
        max_file_size=0,
    )
    return category


def _row(path, category="code"):
    return {"path": path, "extension": "." + path.rsplit(".", 1)[-1],
            "category": category, "is_binary": 0, "size_bytes": 1024}


@pytest.mark.parametrize("ext", sorted(EMBEDDED_SOURCE) + DRIFTED)
def test_inventory_treats_source_format_as_code(tmp_path, ext):
    assert _categorize(tmp_path, f"f{ext}", "x = 1\n") == "code"


def test_inventory_treats_php_include_as_code(tmp_path):
    """CakePHP view partials (app/View/*/fields.inc) are PHP."""
    assert _categorize(tmp_path, "fields.inc", "<?php\necho $vv_name;\n") == "code"


def test_inventory_leaves_non_php_include_alone(tmp_path):
    """.inc is also make/config fragments (dcache's make.inc)."""
    assert _categorize(tmp_path, "make.inc", "CFLAGS = -O2\n") == "other"


@pytest.mark.parametrize("ext", sorted(inv.CODE_EXTENSIONS))
def test_scorer_scores_every_code_extension(ext):
    matcher = score_files.build_cold_prefix_matcher({})
    assert score_files.prefilter_file(_row(f"src/f{ext}"), matcher) is None


def test_scorer_scores_php_include():
    matcher = score_files.build_cold_prefix_matcher({})
    assert score_files.prefilter_file(_row("app/View/Users/fields.inc"), matcher) is None


def test_scorer_still_drops_non_code_include():
    matcher = score_files.build_cold_prefix_matcher({})
    result = score_files.prefilter_file(_row("conf/make.inc", category="other"), matcher)
    assert result is not None and result.signals["rule"] == "non-text"


def test_production_floor_covers_every_code_extension():
    assert inv.CODE_EXTENSIONS <= score_files.PRODUCTION_CODE_EXTENSIONS


def test_worker_language_map_names_only_code_extensions():
    assert set(_EXT_TO_LANG) <= inv.CODE_EXTENSIONS | set(inv.SNIFFED_CODE_EXTENSIONS)


def test_cpg_language_map_names_only_code_extensions():
    assert {"." + ext for ext in cpg_build._EXT_TO_CPG_LANG} <= inv.CODE_EXTENSIONS


@pytest.mark.parametrize("ext,header", sorted(EMBEDDED_SOURCE.items()) + [
    (".inc", "PHP-specific review notes"),
])
def test_worker_gets_language_module_for_embedded_source(ext, header):
    assert header in _compose_language_module(f"src/f{ext}")
