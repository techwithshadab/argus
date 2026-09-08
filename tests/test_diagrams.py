"""The numbers typed into the diagram sources must match the repository (docs/diagrams/src/check_counts.py)."""

import importlib.util
from pathlib import Path

CHECK = Path("docs/diagrams/src/check_counts.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_counts", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diagram_counts_match_repository():
    mod = _load()
    assert mod.problems() == []


def test_every_figure_has_source_and_png():
    for src in Path("docs/diagrams/src").glob("*.html"):
        assert (Path("docs/diagrams") / f"{src.stem}.png").exists(), src.name
