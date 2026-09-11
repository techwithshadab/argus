"""Steps 1-3 of the TrustModel iteration, offline and free.

    python -m services.trustmodel.run_local --latest
    python -m services.trustmodel.run_local --trace artifacts/trustmodel/<id>/trace.json

1. export  a completed investigation to `trace.json`   (needs the database)
2. validate the trace structurally                      (pure, offline)
3. scan     the four MCP servers' 24 tools              (pure, offline)

None of this calls TrustModel or spends a credit. `--trace` re-runs steps 2 and 3 against an
already-exported file, so the checks can be run with no database at all.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .mcp_scan import scan_inventory
from .validate import summarise, validate

REPO = Path(__file__).resolve().parents[2]
TOOLS_JSON = REPO / "mcp-servers" / "tools.json"


def _load_trace(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run(trace_path: Path, out_dir: Path) -> int:
    doc = _load_trace(trace_path)

    print(f"\n[2/3] validating {trace_path}")
    problems = validate(doc)
    stats = summarise(doc)
    for key, value in stats.items():
        print(f"      {key}: {value}")
    if problems:
        print("      PROBLEMS (do not upload):")
        for p in problems:
            print(f"        - {p}")
    else:
        print("      structurally valid; safe to evaluate")

    print("\n[3/3] scanning MCP tool surface")
    scan = scan_inventory(json.loads(TOOLS_JSON.read_text(encoding="utf-8")))
    print(
        f"      {scan['tool_count']} tools across {scan['server_count']} servers "
        f"({', '.join(scan['servers'])})"
    )
    print(f"      checks: {', '.join(scan['checks'])}")
    if scan["clean"]:
        print("      no findings")
    else:
        print(f"      findings by severity: {scan['by_severity']}")
        for f in scan["findings"]:
            print(
                f"        [{f['severity']}] {f['tool']} :: {f['check']} :: {f['detail']}"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "trace": str(trace_path),
        "validation": {"problems": problems, "summary": stats},
        "mcp_scan": scan,
    }
    out = out_dir / "local_checks.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")

    if problems:
        print(
            "\nStep 4 (paid evaluation) is NOT advisable until the problems above are fixed."
        )
        return 1
    print(
        "\nSteps 1-3 complete. Step 4 needs TRUSTMODEL_API_KEY and costs ~1 credit ($100)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TrustModel steps 1-3 (offline, free)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--investigation", help="export this investigation id, then check it"
    )
    group.add_argument(
        "--latest", action="store_true", help="export the latest, then check it"
    )
    group.add_argument("--trace", help="skip the export; check an existing trace.json")
    ap.add_argument("--out", default="artifacts/trustmodel")
    ap.add_argument(
        "--raw", action="store_true", help="embed report text (default: hashes)"
    )
    args = ap.parse_args(argv)

    if args.trace:
        path = Path(args.trace)
        return run(path, path.parent)

    print("[1/3] exporting from the database")
    from .export_trace import fetch, write_trace  # noqa: PLC0415 - needs psycopg
    from .trace_shape import trace_document  # noqa: PLC0415

    row, audit = fetch(args.investigation)
    doc = trace_document(row, audit, redact=not args.raw)
    out_dir = Path(args.out) / str(row["id"])
    path = write_trace(out_dir, doc)
    print(f"      wrote {path}")
    return run(path, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
