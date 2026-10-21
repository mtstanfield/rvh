"""Tests for worker.render_per_task_worker_recipe.

Goose 1.32 cannot accept multi-line --params values (MiniJinja
substitutes raw and the YAML re-parse fails on lines that lose their
literal-block indent). The launcher works around this by pre-rendering
the worker recipe in Python with the prompt baked in as a YAML literal
block, then passing the per-task recipe path to `goose run --recipe`
with no --params.

These tests pin the contract of that pre-render step.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from worker import render_per_task_worker_recipe

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_RECIPE = REPO_ROOT / "prompts" / "recipes" / "worker.yaml"


def test_baked_prompt_round_trips_through_yaml():
    """A multi-line markdown prompt with colons, backticks, and #-headers
    must round-trip through PyYAML cleanly when fed back into yaml.safe_load."""
    prompt = (
        "# Vulnerability analysis task\n"
        "\n"
        "You are reviewing /workspace/src/foo.cpp for security flaws.\n"
        "\n"
        "## Context\n"
        "\n"
        "- Anchor: /workspace/src/foo.cpp\n"
        "- Task ID: 40\n"
        "- Report path: /output/task-40.md\n"
        "\n"
        "Use `xref(symbol=\"eval\")` for step 2.\n"
    )
    rendered = render_per_task_worker_recipe(prompt, BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    # Body is wrapped in {% raw %}…{% endraw %} so goose's MiniJinja pass
    # treats it as literal text; the original prompt must survive verbatim
    # inside the wrapper.
    assert prompt in parsed["prompt"], (
        "round-trip failed: original prompt not found inside baked body"
    )


def test_parameters_block_dropped():
    """The base recipe declares `rendered_prompt` as a required parameter;
    the per-task recipe must drop the block so `goose run` does not
    require `--params`."""
    rendered = render_per_task_worker_recipe("body", BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    assert "parameters" not in parsed, (
        "parameters block leaked into per-task recipe; goose will then "
        "require --params and we'll be back to the multi-line bug"
    )


def test_metadata_preserved():
    """The per-task recipe must keep the title/description and the
    response.json_schema from the base recipe so goose registers
    recipe__final_output with schema validation."""
    rendered = render_per_task_worker_recipe("body", BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    assert "title" in parsed
    assert "response" in parsed, (
        "response.json_schema must survive the per-task render — without "
        "it, goose won't register the schema-validated final_output tool "
        "and the worker has no structured-output contract"
    )
    assert parsed["response"]["json_schema"]["type"] == "object"


def test_template_placeholder_is_replaced():
    """The base recipe has `prompt: |\\n  {{ rendered_prompt }}\\n` —
    after rendering, that exact placeholder must NOT survive in the
    output (otherwise goose would try to substitute again at run time
    and we'd hit the indent bug)."""
    rendered = render_per_task_worker_recipe("substituted body", BASE_RECIPE)
    assert "{{ rendered_prompt }}" not in rendered
    assert "rendered_prompt" not in rendered  # parameter declaration also gone
    parsed = yaml.safe_load(rendered)
    assert "substituted body" in parsed["prompt"]


def test_multi_line_with_no_colons_is_safe():
    """The reproducer from the bug repro: two plain-text lines with no
    colons. This was the canonical break shape for the --params path
    (`line1\\nline2` errored with `Invalid recipe: could not find
    expected ':'`)."""
    prompt = "line1\nline2\nline3 with no colon"
    rendered = render_per_task_worker_recipe(prompt, BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    assert prompt in parsed["prompt"]


def test_double_brace_literals_do_not_become_jinja_params():
    """Worker prompts contain LLM-facing examples like
    `f"SELECT {{user}}"` (worker_python.md line 9). Goose MiniJinja-
    parses the recipe prompt body, so unescaped `{{user}}` is treated
    as an undefined parameter and the recipe is rejected with
    `Missing definitions for parameters in the recipe file: user, var`
    (observed 2026-04-27, all 33 tasks failed in ~0.2s each).

    The bake step wraps the prompt in `{% raw %}…{% endraw %}` AND
    escapes paired braces with U+200B so MiniJinja's raw-block parser
    bug (which still tokenizes `{{...}}` even inside raw) can't match.
    The LLM treats U+200B as invisible, so semantics survive.
    """
    prompt = (
        "Examples of unsafe code:\n"
        "- `f\"SELECT {{user}}\"`\n"
        "- `cursor.execute(f\"...{{var}}...\")`\n"
    )
    rendered = render_per_task_worker_recipe(prompt, BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    body = parsed["prompt"]

    assert "{% raw %}" in body and "{% endraw %}" in body, (
        "prompt body must be wrapped in {% raw %}…{% endraw %} so goose's "
        "MiniJinja pass treats {{user}}/{{var}} as literal text"
    )
    # After ZWSP-stripping, the original placeholder text reappears.
    stripped = body.replace("​", "")
    assert "{{user}}" in stripped, "literal placeholder text must survive into the recipe (modulo ZWSP)"
    assert "{{var}}" in stripped


def test_endraw_in_prompt_raises():
    """If the rendered prompt itself contains the closing `{% endraw %}`
    sentinel, our wrapper would terminate the raw block early and goose
    would re-parse the tail as Jinja. Guard against that with a clear
    error rather than silently producing a broken recipe."""
    poisoned = "before {% endraw %} after"
    with pytest.raises(ValueError, match="endraw"):
        render_per_task_worker_recipe(poisoned, BASE_RECIPE)


# Patterns observed in qwen3 worker_*.md modules that crash MiniJinja's
# raw-block parser even though they're inside {% raw %}…{% endraw %}.
# Reproduced 2026-04-27 against goose 1.30 with adversarial fixtures
# (`{{$where}}` failed, `{{user}}` succeeded). Source: prompts/
# worker_js.md (NoSQL operator examples, prototype-pollution literals)
# and worker_php.md (Twig/Blade `{{ var|raw }}` syntax). The escape
# step in render_per_task_worker_recipe must neutralize all of these.
_MINIJINJA_RAW_BREAKERS = [
    "{{$where}}",            # Mongoose operator — worker_js.md:20
    "{{$regex}}",            # Mongoose operator — worker_js.md:20
    '{{"$ne": null}}',       # NoSQL injection example — worker_js.md:14
    "{{}}",                  # JS object literal — worker_js.md:10
    "{{user: req.body}}",    # JS object literal — worker_js.md:14
    "{{absent, present}}",   # set notation — worker.md:24
    "{{ var|raw }}",         # Twig syntax — worker_php.md
    "{{`backtick`}}",        # backtick inside braces (synthetic)
    "{{{{user}}}}",          # React/Vue example — worker_js.md:37 (quad-brace!)
    "{{{{{ $var }}}}}",      # Blade unescape — worker_php.md:23 (5+5 braces)
    "{{{triple}}}",          # Mustache triple-stash — worker_java.md
]


@pytest.mark.parametrize("pattern", _MINIJINJA_RAW_BREAKERS)
def test_minijinja_raw_breakers_are_neutralized(pattern):
    """MiniJinja's `{% raw %}` block has a parser bug: it still tokenizes
    `{{...}}` inside the block and rejects malformed expressions. Patterns
    like `{{$where}}` or `{{}}` cause goose to error with `Invalid recipe:
    syntax error: unknown statement endraw`. The bake step must escape
    these so MiniJinja sees individual `{` and `}` chars, not a `{{` token.

    We assert the escaped body contains no literal `{{` or `}}` substring
    outside the wrapper sentinels — if that holds, the MiniJinja tokenizer
    can't form an expression and the raw block is unbreakable.
    """
    prompt = f"Example pattern: {pattern}"
    rendered = render_per_task_worker_recipe(prompt, BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    body = parsed["prompt"]

    wrapper_open = "{% raw %}"
    wrapper_close = "{% endraw %}"
    assert body.startswith(wrapper_open)
    assert body.endswith(wrapper_close)
    inner = body[len(wrapper_open):-len(wrapper_close)]
    assert "{{" not in inner, (
        f"escaped body still contains `{{{{` for pattern {pattern!r} — "
        "MiniJinja will reject the recipe"
    )
    assert "}}" not in inner, (
        f"escaped body still contains `}}}}` for pattern {pattern!r} — "
        "MiniJinja will reject the recipe"
    )


def test_escape_uses_zero_width_space_so_llm_sees_original():
    """The escape inserts U+200B (zero-width space) between `{` and `{`.
    The LLM treats U+200B as whitespace-equivalent invisible char, so
    the prompt's *meaning* survives even though MiniJinja can no longer
    tokenize the braces as expression delimiters. Strip U+200B from the
    rendered body and the original prompt must reappear verbatim."""
    prompt = "Mongoose: `{{$where}}` `{{$regex}}` operators"
    rendered = render_per_task_worker_recipe(prompt, BASE_RECIPE)
    parsed = yaml.safe_load(rendered)
    body = parsed["prompt"]
    stripped = body.replace("​", "")
    assert prompt in stripped
