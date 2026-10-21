"""Prompt loader: renders prompts/<name>.md with str.format(**kwargs)."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str, *, cpg_enabled: bool = False, **kwargs) -> str:
    """Load prompts/<name>.md and apply .format(**kwargs).

    Raises FileNotFoundError if the prompt file does not exist.

    Templates use Python .format() substitution syntax. To include a literal
    `{` or `}` in a template (e.g. when showing a JSON example), double it:
    `{{` `}}`. Unescaped braces will raise KeyError at runtime — fail loud,
    not silent.

    When ``cpg_enabled=True`` and a sibling ``<name>_cpg.md`` exists, its
    contents are **prepended** (verbatim — NOT passed through .format(), so
    it does not need to escape braces) to the rendered base prompt, so the
    CPG usage directives land before the base methodology in the model's
    attention window.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8").format(**kwargs)
    if cpg_enabled:
        cpg_path = _PROMPTS_DIR / f"{name}_cpg.md"
        if cpg_path.exists():
            text = (
                cpg_path.read_text(encoding="utf-8").rstrip()
                + "\n\n"
                + text.lstrip()
            )
    return text
