"""Step 4: submit a validated trace to TrustModel, and upload the MCP scan.

Written against the **installed SDK** (`trustmodel==3.6.0`), not the published wiki, which is
wrong in ways that would have failed the call: it documents a `Client` class that does not
exist (the real one is `TrustModelClient`), it documents SDK v2.0.0 against a 3.6.0 release,
and every one of its `evaluate()` examples omits `name`, which is a required argument. See
docs/trustmodel/SDK_FINDINGS.md.

Two things the wiki never mentions and that matter most for governance:

* `frameworks=["owasp-asi", "nist-ai-rmf"]` scores the run against named compliance controls.
  Without it the evaluation returns a bare TrustScore with no control mapping.
* `client.mcp.create_scan()` accepts our own tool-surface findings, so the four Argus MCP
  servers get scan reports in the console instead of a local JSON file.

Nothing spends without an explicit `--confirm`, and the credit balance is checked first so a
run fails *before* a charge rather than during one.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .mcp_scan import SEVERITY_RISK, scan_inventory
from .validate import summarise, validate

ENV_KEY = "TRUSTMODEL_API_KEY"

#: Compliance frameworks to score the trajectory against. These two slugs ship in the package;
#: `--probe` lists what this account actually has via `client.frameworks.list()`.
DEFAULT_FRAMEWORKS = ("owasp-asi", "nist-ai-rmf")

REPO = Path(__file__).resolve().parents[2]
TOOLS_JSON = REPO / "mcp-servers" / "tools.json"

# Severity vocabulary and risk scores live in mcp_scan.py, which is the module that emits
# them; keeping a second copy here is exactly the drift that broke SENSORS.


def api_key() -> str:
    key = os.getenv(ENV_KEY, "").strip()
    if not key:
        raise SystemExit(
            f"{ENV_KEY} is not set. Put it in .env or the environment; never in CDK context."
        )
    return key


def client(key: str | None = None):
    """The real client. `TrustModelClient`, not the wiki's `Client`."""
    from trustmodel import TrustModelClient  # noqa: PLC0415

    return TrustModelClient(api_key=key or api_key(), agent_id="argus-orchestrator")


def probe() -> dict:
    """What the installed SDK and this account actually offer. Free; spends nothing."""
    try:
        import trustmodel  # noqa: PLC0415
    except ImportError:
        return {
            "installed": False,
            "hint": "pip install -r services/trustmodel/requirements.txt",
        }

    out: dict = {
        "installed": True,
        "version": getattr(trustmodel, "__version__", "unknown"),
        "client": "TrustModelClient",
    }
    if not os.getenv(ENV_KEY, "").strip():
        out["note"] = f"{ENV_KEY} unset; skipping the account calls"
        return out

    c = client()
    for label, call in (
        ("credits", lambda: c.credits.get_balance().model_dump()),
        ("pricing", lambda: c.agentic.get_pricing().model_dump()),
        ("frameworks", lambda: [f.model_dump() for f in c.frameworks.list()]),
        ("framework_domains", lambda: c.frameworks.list_domains()),
    ):
        try:
            out[label] = call()
        except Exception as e:  # noqa: BLE001 - a probe reports failures, never raises
            out[label] = f"unavailable: {type(e).__name__}: {e}"
    return out


def affordable(c) -> tuple[bool, str]:
    """Check the balance before uploading, so a run fails before a charge, not during."""
    try:
        bal = c.credits.get_balance()
    except Exception as e:  # noqa: BLE001
        return True, f"balance unknown ({type(e).__name__}); proceeding"
    remaining = getattr(bal, "total_credits_available", None)
    if remaining is None:
        remaining = getattr(bal, "credits_remaining", None)
    if remaining is not None and remaining <= 0:
        return False, f"no credits remaining (used {getattr(bal, 'credits_used', '?')})"
    return True, f"{remaining} credits available"


def submit(
    trace_path: Path,
    name: str | None = None,
    frameworks: tuple[str, ...] = DEFAULT_FRAMEWORKS,
) -> dict:
    """Upload and evaluate one trace. This is the call that spends."""
    doc = json.loads(trace_path.read_text(encoding="utf-8"))
    problems = validate(doc)
    if problems:
        raise SystemExit(
            "refusing to spend a credit on an invalid trace:\n  - "
            + "\n  - ".join(problems)
        )

    c = client()
    ok, why = affordable(c)
    if not ok:
        raise SystemExit(f"not submitting: {why}")

    meta = doc.get("metadata") or {}
    run = c.agentic.evaluate(
        file_path=str(trace_path),
        # Required, and absent from every example in the published wiki.
        name=name
        or f"argus-investigation-{meta.get('investigation_id', 'unknown')[:8]}",
        goal=doc["goal"],
        agent_framework=doc.get("agent_framework") or "langgraph",
        agent_model=doc.get("agent_model"),
        goal_achieved=bool(doc.get("goal_achieved")),
        frameworks=list(frameworks),
        agent_id="argus-orchestrator",
    )
    return {
        "balance_before": why,
        "evaluation_run_id": getattr(run, "id", None)
        or getattr(run, "evaluation_run_id", None),
        "frameworks": list(frameworks),
        "run": run.model_dump() if hasattr(run, "model_dump") else str(run),
    }


def mcp_findings_for(server: str, scan: dict, tool_names: list[str]) -> list:
    """One McpFinding per tool -- including the clean ones.

    The API requires `len(findings) == total_tools` (verified against a real 400:
    "findings length (0) must equal total_tools (10)"). That is the right contract: a scan
    report is evidence that every tool was examined, so a clean tool is reported as
    `safe=True, risk_score=0` rather than omitted.
    """
    from trustmodel.models.mcp_scanner import McpFinding, McpThreat  # noqa: PLC0415

    by_tool: dict[str, list[dict]] = {}
    for f in scan["findings"]:
        if f["server"] == server:
            by_tool.setdefault(f["tool"], []).append(f)

    out = []
    for name in sorted(tool_names):
        dotted = f"{server}.{name}"
        findings = by_tool.get(dotted, [])
        if not findings:
            out.append(
                McpFinding(tool_name=dotted, risk_score=0, safe=True, threats=[])
            )
            continue
        worst = max(findings, key=lambda f: SEVERITY_RISK.get(f["severity"], 0))
        out.append(
            McpFinding(
                tool_name=dotted,
                risk_score=SEVERITY_RISK.get(worst["severity"], 0),
                safe=False,
                threats=[
                    McpThreat(
                        type=f["check"],
                        severity=f["severity"],
                        description=f["detail"],
                        evidence=f["tool"],
                    )
                    for f in findings
                ],
            )
        )
    return out


def upload_mcp_scan() -> list[dict]:
    """Push the local tool-surface scan up as one scan report per MCP server.

    A clean result is not a non-result: `ok` with zero findings across 24 tools is exactly the
    evidence a governance reviewer wants, and it is only evidence once it is recorded.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    inventory = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))
    scan = scan_inventory(inventory)
    c = client()
    scanned_at = datetime.now(UTC).isoformat()
    results = []

    for server in scan["servers"]:
        tool_names = [
            str(t.get("name") or "")
            for t in inventory["servers"][server].get("tools") or []
        ]
        findings = mcp_findings_for(server, scan, tool_names)
        tools = len(tool_names)
        worst = "none"
        for f in findings:
            for t in f.threats or []:
                if SEVERITY_RISK.get(t.severity, 0) > SEVERITY_RISK.get(worst, 0):
                    worst = t.severity
        # The server rejects worst_severity="none" even though `McpScanSeverity` lists it:
        # a clean scan must send null. Verified against a real 400:
        #   {"worst_severity": ["must be one of low/medium/high/critical or null; got 'none'."]}
        report = c.mcp.create_scan(
            server_name=f"argus-{server}",
            # Every tool yields a finding now, so status keys on unsafe ones, not count.
            status="ok" if all(f.safe for f in findings) else "warning",
            total_tools=tools,
            blocked_tools=0,
            worst_severity=None if worst == "none" else worst,
            findings=findings,
            scanned_at=scanned_at,
            metadata={"source": "argus", "checks": scan["checks"]},
        )
        results.append(
            {
                "server": server,
                "scan_id": getattr(report, "id", None),
                "status": getattr(report, "status", None),
                "total_tools": tools,
                "findings": len(findings),
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TrustModel step 4 (spends credits)")
    ap.add_argument("--trace", help="path to a validated trace.json")
    ap.add_argument(
        "--probe", action="store_true", help="inspect SDK, credits, frameworks"
    )
    ap.add_argument(
        "--mcp-scan", action="store_true", help="upload the MCP tool-surface scan"
    )
    ap.add_argument(
        "--name", help="evaluation name (required by the API; defaulted if unset)"
    )
    ap.add_argument(
        "--frameworks",
        default=",".join(DEFAULT_FRAMEWORKS),
        help="comma-separated compliance slugs to score against",
    )
    ap.add_argument(
        "--confirm", action="store_true", help="required: this spends credits"
    )
    args = ap.parse_args(argv)

    if args.probe:
        print(json.dumps(probe(), indent=2, default=str))
        return 0

    if args.mcp_scan:
        if not args.confirm:
            print("Dry run. Re-run with --confirm to upload the MCP scan.")
            return 0
        for row in upload_mcp_scan():
            print(json.dumps(row, default=str))
        return 0

    if not args.trace:
        ap.error("one of --trace, --probe or --mcp-scan is required")

    path = Path(args.trace)
    doc = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(summarise(doc), indent=2))
    problems = validate(doc)
    if problems:
        print("\nPROBLEMS (will not submit):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if not args.confirm:
        print("\nDry run. Re-run with --confirm to submit (this spends credits).")
        return 0

    result = submit(
        path,
        args.name,
        tuple(s.strip() for s in args.frameworks.split(",") if s.strip()),
    )
    out = path.parent / "evaluation_run.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nevaluation_run_id: {result['evaluation_run_id']}\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
