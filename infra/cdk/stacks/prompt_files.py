"""The prompt files and their placeholders, dependency-free so unit tests can pin the rule
the deploy uses (`stacks/prompts.py`). Prompts are `str.format` templates: single-brace
`{name}` is an input variable, doubled braces are literal JSON."""

from __future__ import annotations

import re
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")


def prompt_files(root: str | Path) -> dict[str, Path]:
    return {
        p.stem: p
        for p in sorted((Path(root) / "agents" / "shared" / "prompts").glob("*.md"))
    }


def placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text)))
