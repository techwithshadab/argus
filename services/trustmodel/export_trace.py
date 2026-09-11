"""Read one Argus investigation from the database and write a TrustModel `trace.json`.

Thin by design: every shaping decision lives in `trace_shape.py`, which is pure and unit
tested. This module only does the two things a unit test must not -- open a connection and
write a file.

`db_url()` is duplicated verbatim across the images (CLAUDE.md), so this reuses the API's
`dbconn` rather than adding a fourth copy.

Usage:
    python -m services.trustmodel.export_trace --investigation <uuid> --out artifacts/
    python -m services.trustmodel.export_trace --latest --out artifacts/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .trace_shape import trace_document

INVESTIGATION_SQL = """
SELECT id, mmsi, status, trigger, report, manifest, trace_id,
       review_state, reviewed_by, reviewed_at, created_at, updated_at
FROM investigations
WHERE id = %s
"""

LATEST_SQL = """
SELECT id, mmsi, status, trigger, report, manifest, trace_id,
       review_state, reviewed_by, reviewed_at, created_at, updated_at
FROM investigations
WHERE status = 'complete' AND manifest IS NOT NULL
ORDER BY created_at DESC
LIMIT 1
"""

AUDIT_SQL = """
SELECT ts, actor, actor_kind, action, entity_kind, entity_id, details, trace_id
FROM audit_events
WHERE (entity_kind = 'investigation' AND entity_id = %s)
   OR (trace_id IS NOT NULL AND trace_id = %s)
ORDER BY ts ASC
"""


def _pool():
    """The API's rotating pool, so a password rotation is handled the same way here."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
    from dbconn import RotatingPool  # noqa: PLC0415

    return RotatingPool()


def fetch(investigation_id: str | None) -> tuple[dict, list[dict]]:
    """One investigation row and its audit trail."""
    pool = _pool()
    with pool.connection() as conn, conn.cursor() as cur:
        if investigation_id:
            cur.execute(INVESTIGATION_SQL, (investigation_id,))
        else:
            cur.execute(LATEST_SQL)
        row = cur.fetchone()
        if not row:
            raise SystemExit(
                f"no investigation found for {investigation_id!r}"
                if investigation_id
                else "no completed investigation with a manifest"
            )
        cur.execute(AUDIT_SQL, (str(row["id"]), row.get("trace_id")))
        audit = list(cur.fetchall())
    return dict(row), [dict(a) for a in audit]


def write_trace(out_dir: Path, doc: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trace.json"
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Export an Argus investigation for TrustModel"
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--investigation", help="investigation id (uuid)")
    group.add_argument(
        "--latest", action="store_true", help="most recent completed investigation"
    )
    ap.add_argument("--out", default="artifacts/trustmodel", help="output directory")
    ap.add_argument(
        "--raw",
        action="store_true",
        help=(
            "embed report text instead of hashes. Off by default: personal data is encrypted "
            "at rest and the API stays pseudonymous, so raw upload is a deliberate choice."
        ),
    )
    args = ap.parse_args(argv)

    row, audit = fetch(args.investigation)
    doc = trace_document(row, audit, redact=not args.raw)

    out_dir = Path(args.out) / str(row["id"])
    path = write_trace(out_dir, doc)

    spans = doc["spans"]
    human = sum(1 for s in spans if s["metadata"].get("human_decision"))
    tools = sum(len(s["tool_calls"]) for s in spans)
    print(f"wrote {path}")
    print(
        f"  investigation {row['id']} mmsi={row.get('mmsi')} status={row.get('status')}\n"
        f"  {len(spans)} spans, {tools} tool calls, {human} human decisions, "
        f"redacted={doc['metadata']['redacted']}"
    )
    if not doc["metadata"]["code_revision"]:
        print("  note: manifest has no code_revision (GIT_SHA unset at build)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
