"""Step 4: submit a validated trace to TrustModel. Costs about 1 credit ($100).

Deliberately guarded. The wiki's quickstart and its API reference disagree about the client
surface -- the quickstart shows `Client(...).agentic.evaluate(...)`, the API reference shows
`TrustModelClient(...).evaluations.create(...)` -- so rather than guess, `_evaluator()` probes
the installed package and reports what it found. That check is free; only `submit()` spends.

Nothing here runs without an explicit `--confirm`, because a malformed upload still costs.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .validate import summarise, validate

ENV_KEY = "TRUSTMODEL_API_KEY"


def api_key() -> str:
    key = os.getenv(ENV_KEY, "").strip()
    if not key:
        raise SystemExit(
            f"{ENV_KEY} is not set. Put it in .env or the environment; never in CDK context."
        )
    return key


def _client(key: str):
    """Whichever client class the installed package actually exposes."""
    import trustmodel  # noqa: PLC0415

    for name in ("Client", "TrustModelClient"):
        cls = getattr(trustmodel, name, None)
        if cls is not None:
            return cls(api_key=key), name
    raise SystemExit(
        "installed trustmodel package exposes neither Client nor TrustModelClient; "
        "run `python -m services.trustmodel.evaluate --probe` and read the output"
    )


def probe() -> dict:
    """What the installed SDK offers. Free, no upload, no key needed to inspect."""
    try:
        import trustmodel  # noqa: PLC0415
    except ImportError:
        return {"installed": False, "hint": "pip install trustmodel"}
    surfaces = {}
    for name in ("Client", "TrustModelClient"):
        cls = getattr(trustmodel, name, None)
        if cls is not None:
            surfaces[name] = sorted(a for a in dir(cls) if not a.startswith("_"))
    return {
        "installed": True,
        "version": getattr(trustmodel, "__version__", "unknown"),
        "top_level": sorted(a for a in dir(trustmodel) if not a.startswith("_")),
        "client_surfaces": surfaces,
    }


def submit(trace_path: Path, goal: str | None = None) -> dict:
    """Upload and evaluate. This is the call that spends a credit."""
    doc = json.loads(trace_path.read_text(encoding="utf-8"))
    problems = validate(doc)
    if problems:
        raise SystemExit(
            "refusing to spend a credit on an invalid trace:\n  - "
            + "\n  - ".join(problems)
        )

    client, surface = _client(api_key())
    kwargs = {
        "file_path": str(trace_path),
        "goal": goal or doc.get("goal"),
        "agent_framework": doc.get("agent_framework"),
        "agent_model": doc.get("agent_model"),
        "goal_achieved": bool(doc.get("goal_achieved")),
    }

    agentic = getattr(client, "agentic", None)
    if agentic is not None and hasattr(agentic, "evaluate"):
        run = agentic.evaluate(**kwargs)
    else:
        evaluations = getattr(client, "evaluations", None)
        if evaluations is None:
            raise SystemExit(
                f"{surface} exposes no agentic/evaluations surface; run --probe"
            )
        run = evaluations.create(**kwargs)

    return {
        "surface": surface,
        "evaluation_run_id": getattr(run, "evaluation_run_id", None)
        or getattr(run, "id", None),
        "raw": getattr(run, "__dict__", {}) or {},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TrustModel step 4 (costs ~1 credit)")
    ap.add_argument("--trace", help="path to a validated trace.json")
    ap.add_argument(
        "--probe", action="store_true", help="inspect the installed SDK; free"
    )
    ap.add_argument(
        "--confirm", action="store_true", help="required: this spends a credit"
    )
    args = ap.parse_args(argv)

    if args.probe:
        print(json.dumps(probe(), indent=2))
        return 0

    if not args.trace:
        ap.error("--trace is required unless --probe")

    path = Path(args.trace)
    doc = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(summarise(doc), indent=2))

    if not args.confirm:
        print(
            "\nDry run. Re-run with --confirm to submit (this spends ~1 credit / $100)."
        )
        return 0

    result = submit(path)
    out = path.parent / "evaluation_run.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nevaluation_run_id: {result['evaluation_run_id']}\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
