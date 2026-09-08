#!/usr/bin/env python3
"""Verify the numbers typed into the diagram sources against the repository.

Figures state concrete counts (4 MCP servers, 24 tools, 8 migrations...). They are hand-typed into
the HTML, so they drift the moment a tool or migration is added. Every such number is wrapped as
``<span data-count="key">N</span>``; this script derives each key from the code and data and fails
if a figure disagrees, enforcing the rule that docs claim only what an artifact backs. When it
fails, update the number in the named figure and re-render (``make diagrams``).

    python docs/diagrams/src/check_counts.py     # exits non-zero on any mismatch

Runs in CI through tests/test_diagrams.py with nothing beyond the standard library and PyYAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(__file__).resolve().parent
SCENARIO = ROOT / "data" / "scenarios" / "east_med_baseline.yaml"
COUNT_RE = re.compile(r'data-count="([a-z_]+)">(\d+)<')


def scenario_counts() -> tuple[int, int]:
    """(vessels, injected anomalies) of the demo scenario, from the generator itself."""
    sys.path.insert(0, str(ROOT))
    from data.synthetic.generator import ScenarioGenerator

    gen = ScenarioGenerator.from_file(str(SCENARIO)).run()
    return len(gen.vessels()), len(gen.ground_truth())


def repo_counts() -> dict[str, int]:
    servers = [
        p
        for p in (ROOT / "mcp-servers" / "servers").glob("*.py")
        if p.name != "__init__.py"
    ]
    tools = sum(p.read_text().count("@mcp.tool") for p in servers)
    agents = [
        p
        for p in (ROOT / "agents").iterdir()
        if p.is_dir() and p.name != "shared" and (p / "app.py").exists()
    ]
    migrations = [
        p for p in (ROOT / "data" / "sql").glob("*.sql") if not p.name.startswith("000")
    ]
    adrs = [p for p in (ROOT / "docs" / "adr").glob("[0-9]*.md")]
    vessels, anomalies = scenario_counts()
    return {
        "mcp_servers": len(servers),
        "mcp_tools": tools,
        "agents": len(agents),
        "migrations": len(migrations),
        "adrs": len(adrs),
        "vessels": vessels,
        "anomalies": anomalies,
    }


def problems() -> list[str]:
    truth = repo_counts()
    out: list[str] = []
    seen: set[str] = set()
    for html in sorted(SRC.glob("*.html")):
        for key, said in COUNT_RE.findall(html.read_text()):
            seen.add(key)
            if key not in truth:
                out.append(f"{html.name}: unknown count key {key!r}")
            elif int(said) != truth[key]:
                out.append(
                    f"{html.name} {key}: says {said}, repository has {truth[key]}"
                )
    return out


def main() -> int:
    truth = repo_counts()
    print("repository:", ", ".join(f"{k}={v}" for k, v in truth.items()))
    found = problems()
    for p in found:
        print("STALE:", p)
    print(
        "OK: every diagram count matches"
        if not found
        else f"{len(found)} stale count(s)"
    )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
