"""Tree rendering for analyze_repo: full-tree mode with extension grouping.

The earlier budgeted pruner (max_entries fair-share) caused the coriolix
2026-05-03 failure: vendored library filenames (`jquery.min.js`,
`leaflet@1.2.0.js`) sat below the budget cap and the analyzer couldn't
classify `gui/static/ancillary/js/` as `generated_paths`. The current
renderer drops the budget entirely — real first-party repos after
SKIP_DIRS pruning fit comfortably in context — and uses extension
grouping to compress repetitive runs (50 image files render as
`[50 .png files: …]`) while always surfacing distinctive files
(vendored libraries, build manifests, docs).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from repo_tree import build_repo_tree  # noqa: E402


def _make_repo(root: Path, layout: dict) -> None:
    """Create files/dirs from a nested-dict layout. Strings are file content,
    dicts are subdirs. Empty values create empty files."""
    for name, value in layout.items():
        target = root / name
        if isinstance(value, dict):
            target.mkdir(parents=True, exist_ok=True)
            _make_repo(target, value)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value or "", encoding="utf-8")


def test_vendored_filenames_surface_at_depth(tmp_path):
    """Vendored libraries deep in static/ must surface — they're the
    primary signal for `generated_paths` classification."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "gui": {
            "static": {
                "ancillary": {
                    "js": {
                        "jquery.min.js": "// vendor",
                        "leaflet@1.2.0.js": "// vendor",
                        "bootstrap.bundle.min.js": "// vendor",
                    },
                },
                "bootstrap": {
                    "css": {"bootstrap.min.css": "/* vendor */"},
                },
            },
            "views.py": "from django.shortcuts import render\n",
        },
    })
    tree = build_repo_tree(repo)
    assert "jquery.min.js" in tree
    assert "leaflet@1.2.0.js" in tree
    assert "bootstrap.min.css" in tree


def test_skip_dirs_still_skipped(tmp_path):
    """node_modules/ never enumerated, marker present."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "static": {
            "node_modules": {
                "jquery": {"package.json": "{}"},
            },
            "css": {"main.css": "body { }"},
        },
    })
    tree = build_repo_tree(repo)
    assert "(skipped)" in tree
    assert "main.css" in tree
    assert "package.json" not in tree


def test_asset_sibling_names_surface_alongside_blob_directory(tmp_path):
    """Coriolix 2026-05-03 reproducer. A sibling directory packed with
    50 images must NOT hide other asset-dir siblings — the renderer
    surfaces all of them."""
    repo = tmp_path / "myrepo"
    image_blob = {f"image{i:03d}.png": "binary" for i in range(50)}
    _make_repo(repo, {
        "gui": {
            "static": {
                "3rdPartyDataAccessFiles": image_blob,
                "ancillary": {
                    "js": {
                        "jquery.min.js": "// vendor",
                        "leaflet@1.2.0.js": "// vendor",
                    },
                },
                "bootstrap": {
                    "css": {"bootstrap.min.css": "/* vendor */"},
                },
            },
            "views.py": "from django.shortcuts import render\n",
        },
    })
    tree = build_repo_tree(repo)
    assert "ancillary/" in tree
    assert "bootstrap/" in tree
    assert "3rdPartyDataAccessFiles/" in tree
    assert "jquery.min.js" in tree
    assert "leaflet@1.2.0.js" in tree


def test_extension_grouping_compresses_repetitive_runs(tmp_path):
    """A directory of 50 PNGs must render as a grouped count, not 50
    individual lines, so the tree stays compact in context."""
    repo = tmp_path / "myrepo"
    image_blob = {f"image{i:03d}.png": "binary" for i in range(50)}
    _make_repo(repo, {
        "static": {
            "3rdPartyDataAccessFiles": image_blob,
        },
    })
    tree = build_repo_tree(repo)
    assert "[50 .png files:" in tree
    # Grouped form should NOT enumerate each file
    rendered_lines = tree.splitlines()
    image_lines = [ln for ln in rendered_lines if ln.strip().startswith("image")]
    assert len(image_lines) == 0, (
        f"Expected grouping; got individual lines:\n{image_lines}"
    )


def test_distinctive_files_render_individually_even_among_many(tmp_path):
    """Vendored libraries inside a JS directory must not be lost in
    extension grouping — distinctive detection pulls them out first."""
    repo = tmp_path / "myrepo"
    files = {f"app_module_{i}.js": "// app" for i in range(10)}
    files["jquery.min.js"] = "// vendor"
    files["leaflet@1.2.0.js"] = "// vendor"
    _make_repo(repo, {"static": {"js": files}})
    tree = build_repo_tree(repo)
    # Distinctive vendored libs surface individually
    assert "jquery.min.js" in tree
    assert "leaflet@1.2.0.js" in tree
    # Remaining .js files (10 app modules) get grouped
    assert "[10 .js files:" in tree


def test_small_extension_groups_render_individually(tmp_path):
    """A directory with 3 .py files should render each individually
    (under the GROUP_THRESHOLD=4 cutoff)."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "src": {
            "views.py": "x = 1\n",
            "models.py": "y = 2\n",
            "urls.py": "z = 3\n",
        },
    })
    tree = build_repo_tree(repo)
    assert "views.py" in tree
    assert "models.py" in tree
    assert "urls.py" in tree
    # Should not collapse to a [3 .py files: …] group
    assert "[3 .py files" not in tree


def test_max_depth_respected(tmp_path):
    """The max_depth safety rail still bounds runaway recursion."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "src": {
            "d1": {"d2": {"d3": {"d4": {"deeply_nested.py": "x = 1\n"}}}},
        },
    })
    # max_depth=3 cuts off before the depth-5 file renders
    tree = build_repo_tree(repo, max_depth=3)
    assert "deeply_nested.py" not in tree
    # default max_depth=8 lets it through
    tree = build_repo_tree(repo)
    assert "deeply_nested.py" in tree


def test_build_files_marked_with_star(tmp_path):
    """Build manifests still get the ` (*)` marker so the model can spot them."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "Makefile": "all:\n",
        "package.json": "{}",
        "src": {"main.py": "x = 1\n"},
    })
    tree = build_repo_tree(repo)
    assert "Makefile (*)" in tree
    assert "package.json (*)" in tree


def test_docs_render_individually(tmp_path):
    """README, LICENSE and *.md files always render individually as docs."""
    repo = tmp_path / "myrepo"
    _make_repo(repo, {
        "README.md": "",
        "LICENSE": "",
        "CHANGELOG.md": "",
        "docs": {
            "intro.md": "",
            "guide.md": "",
            "api.md": "",
            "reference.md": "",
            "tutorial.md": "",
        },
    })
    tree = build_repo_tree(repo)
    assert "README.md" in tree
    assert "LICENSE" in tree
    assert "CHANGELOG.md" in tree
    # All .md files should appear individually as docs (no grouping)
    assert "[5 .md files" not in tree
