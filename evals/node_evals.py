"""Per-node evals with regression thresholds (phase 5).

    python evals/node_evals.py --api http://localhost:8000 [--suites watch,investigator,tasking,report] [--gate] [--push]

Runs against a live stack. Cases live in evals/cases.yaml, thresholds in evals/thresholds.yaml.

Suites
- watch:        queue a sweep; score raised alerts against the scenario's ground truth
                (recall, precision, per-kind recall). Deterministic scoring.
- investigator: send scoped requests straight to the Investigator over A2A; score schema
                validity, evidence traceability (every evidence source is a real tool), and
                expected-content keywords per case.
- tasking:      send an evidence gap to the Tasking agent; score schema validity and whether
                the recommendation matches the expected decision.
- report:       open an investigation through the API (the full graph), then score the report
                with the policy checks and an LLM judge rubric (1-5) on the Bedrock strong tier.

Results go to evals/results/<timestamp>.json, to the platform (POST /evals) so the eval-recall
SLO reads them, and optionally to CloudWatch metrics (--push, namespace Argus/Evals). With --gate the process exits 1 when any
threshold is missed, which is what the evals workflow relies on."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from run_eval import http  # noqa: E402
from run_eval import score as score_alerts  # noqa: E402
from scoring import (  # noqa: E402
    check_thresholds,
    evidence_traceability,
    judge_consensus,
    keyword_hits,
    rubric_average,
)


# ---------------- helpers ----------------
def a2a_send(url: str, text: str, timeout: float = 600) -> str:
    req = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    r = http().post(url, json=req, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(json.dumps(body["error"])[:300])
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("kind") == "text" and isinstance(x.get("text"), str):
                out.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(body.get("result", body))
    return "\n".join(out)


def json_object(text: str) -> dict:
    """The agents' own parser: first complete object, prose after it ignored."""
    sys.path.insert(0, str(HERE.parent / "agents"))
    from shared.graph import json_object as parse  # noqa: PLC0415

    return parse(text)


def wait_job(api: str, job_id: str, timeout: float) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = http().get(f"{api}/jobs/{job_id}", timeout=30).json()
        if j["status"] in ("succeeded", "failed", "dead"):
            return j
        time.sleep(5)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


# ---------------- suites ----------------
def wait_for_idle(api: str, timeout: float = 900) -> bool:
    """Wait until no job is queued or running. Sweeps share the worker with investigations
    (which earlier sweeps may have auto-opened), and a sweep queued behind a backlog would
    time out here while telling us nothing about the Watch agent."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        busy = 0
        for st in ("queued", "running"):
            try:
                jobs = (
                    http()
                    .get(f"{api}/jobs", params={"status": st, "limit": 50}, timeout=30)
                    .json()
                )
            except Exception:  # noqa: BLE001
                busy += 1
                continue
            # A job "running" for longer than any job may take was orphaned by a worker
            # restart; it is not load and must not hold the sweep back.
            for j in jobs:
                stamp = j.get("started_at") or j.get("created_at") or ""
                try:
                    age = (
                        datetime.now(UTC)
                        - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    ).total_seconds()
                except ValueError:
                    age = 0
                if age < 1800:
                    busy += 1
        if busy == 0:
            return True
        time.sleep(10)
    return False


def suite_watch(api: str, cfg: dict) -> dict:
    if not wait_for_idle(api):
        print(
            "  worker still busy after 15 min; sweep will queue behind it",
            file=sys.stderr,
        )
    truth = http().get(f"{api}/ground-truth", timeout=30).json()
    existing = http().get(f"{api}/alerts", timeout=30).json()
    # Clean slate: the Watch agent deliberately does not duplicate open alerts, so alerts
    # left by earlier sweeps would hide detections. Dismiss them as the evals officer.
    for a in existing:
        if a.get("status") == "open":
            http().post(
                f"{api}/alerts/{a['id']}/review",
                json={"decision": "rejected", "note": "eval reset"},
                headers={"X-Watch-Officer": "evals"},
                timeout=30,
            )
    before = {a["id"] for a in existing}
    job = (
        http()
        .post(
            f"{api}/sweep",
            params={"hours": cfg.get("hours", 12)},
            headers={"X-Watch-Officer": "evals"},
            timeout=30,
        )
        .json()
    )
    wait_job(api, job["job_id"], cfg.get("timeout_s", 900))
    alerts = [
        a
        for a in http().get(f"{api}/alerts", timeout=30).json()
        if a["id"] not in before
    ]
    s = score_alerts(truth, alerts)
    return {
        "recall": s["recall"],
        "precision": s["precision"],
        "recall_by_kind": s["recall_by_kind"],
        "alerts": len(alerts),
        "truth": len(truth),
    }


def suite_investigator(cfg: dict) -> dict:
    url = cfg["a2a_url"]
    results = []
    for case in cfg["cases"]:
        msg = f"Scope: {case['scope']}\nVessel MMSI: {case['mmsi']}\nTrigger: {case.get('trigger', 'manual')}\nTriggering alert: {json.dumps(case.get('alert', {}))}\nReturn the InvestigationFindings JSON for your scope only."
        t0 = time.time()
        try:
            data = json_object(a2a_send(url, msg, cfg.get("timeout_s", 600)))
            valid = "error" not in data and all(
                k in data
                for k in (
                    "identity",
                    "behaviour_summary",
                    "risk_indicators",
                    "evidence",
                    "confidence",
                )
            )
        except Exception as e:  # noqa: BLE001
            data, valid = {"error": str(e)}, False
        text = json.dumps(data)
        results.append(
            {
                "case": case["name"],
                "schema_valid": valid,
                "evidence_traceability": evidence_traceability(data.get("evidence", []))
                if valid
                else 0.0,
                "expected_hits": keyword_hits(text, case.get("expect", [])),
                "unexpected_absent": 1.0
                - keyword_hits(text, case.get("expect_absent", []))
                if case.get("expect_absent")
                else 1.0,
                "latency_s": round(time.time() - t0, 1),
                "tier": (data.get("provenance") or {}).get("tier"),
                "attempts": (data.get("provenance") or {}).get("attempts"),
            }
        )
    n = max(len(results), 1)
    return {
        "schema_valid": round(sum(r["schema_valid"] for r in results) / n, 3),
        "evidence_traceability": round(
            sum(r["evidence_traceability"] for r in results) / n, 3
        ),
        "expected_hits": round(sum(r["expected_hits"] for r in results) / n, 3),
        "cases": results,
    }


def tasking_payload(obj: dict, mmsi: int) -> dict:
    """The same coercion the orchestrator applies (schema-shaped answers, missing mmsi)."""
    sys.path.insert(0, str(HERE.parent / "agents"))
    from shared.graph import tasking_payload as coerce  # noqa: PLC0415

    return coerce(obj, mmsi)


def suite_tasking(cfg: dict) -> dict:
    url = cfg["a2a_url"]
    results = []
    for case in cfg["cases"]:
        msg = f"Vessel MMSI: {case['mmsi']}\nBehaviour summary: {case['behaviour']}\nEvidence gap (position and time where evidence is missing): {case['gap']}\nDecide whether collection would help and, if so, propose it. Return the TaskingRecommendation JSON."
        try:
            data = tasking_payload(
                json_object(a2a_send(url, msg, cfg.get("timeout_s", 600))), case["mmsi"]
            )
            valid = "recommended" in data and "rationale" in data
        except Exception as e:  # noqa: BLE001
            data, valid = {"error": str(e)}, False
        results.append(
            {
                "case": case["name"],
                "schema_valid": valid,
                "decision_match": valid
                and bool(data.get("recommended")) == bool(case["expect_recommended"]),
            }
        )
    n = max(len(results), 1)
    return {
        "schema_valid": round(sum(r["schema_valid"] for r in results) / n, 3),
        "decision_match": round(sum(r["decision_match"] for r in results) / n, 3),
        "cases": results,
    }


RUBRIC = """You are grading a Vessel of Interest report written for a maritime watch officer. Score 1 to 5 on each:
- bluf: is the headline a clear bottom line up front?
- traceability: does every indicator visibly rest on cited evidence, with no invented facts?
- balance: are counter-indicators and information gaps stated honestly?
- actionability: are recommended actions concrete things a watch officer can do, phrased as advice?
- clarity: can someone with 90 seconds understand it?
Return ONLY JSON: {"bluf": n, "traceability": n, "balance": n, "actionability": n, "clarity": n, "comment": "..."}"""


def judge(report: dict, model_id: str, region: str, samples: int = 3) -> dict | None:
    """LLM judge on Bedrock (converse API), sampled `samples` times at temperature 0 and
    averaged per dimension: one sample swung the rubric by a full point between identical
    runs. None when no credentials are available."""
    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=region)
        votes: list[dict] = []
        for _ in range(max(1, samples)):
            resp = client.converse(
                modelId=model_id,
                system=[{"text": RUBRIC}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": json.dumps(report, indent=1)[:12000]}],
                    }
                ],
                inferenceConfig={"maxTokens": 400, "temperature": 0.0},
            )
            text = resp["output"]["message"]["content"][0]["text"]
            votes.append(json_object(text))
        return judge_consensus(votes)
    except Exception as e:  # noqa: BLE001
        print(f"  judge unavailable: {e}", file=sys.stderr)
        return None


def suite_report(api: str, cfg: dict, judge_model: str, region: str) -> dict:
    sys.path.insert(0, str(HERE.parent / "agents"))
    from shared.policy import validate_report  # noqa: PLC0415

    results = []
    for case in cfg["cases"]:
        r = (
            http()
            .post(
                f"{api}/investigations/{case['mmsi']}",
                json={"trigger": case.get("trigger", "eval")},
                headers={"X-Watch-Officer": "evals"},
                timeout=30,
            )
            .json()
        )
        job = wait_job(api, r["job_id"], cfg.get("timeout_s", 1200))
        inv = (
            http()
            .get(f"{api}/investigations/{r['investigation_id']}", timeout=30)
            .json()
        )
        ok = job["status"] == "succeeded" and inv.get("status") == "complete"
        rep = inv.get("report") or {}
        # The sources the specialists actually cited, from the evidence the platform
        # froze for this case. Passing an empty set made `check_evidence` skip the
        # traceability check entirely, so the report suite never scored it (A6).
        sources: set[str] = set()
        if ok:
            try:
                sources = {
                    (row.get("source") or "").strip()
                    for row in http()
                    .get(
                        f"{api}/evidence",
                        params={
                            "entity_kind": "investigation",
                            "entity_id": r["investigation_id"],
                        },
                        timeout=30,
                    )
                    .json()
                    if (row.get("source") or "").strip()
                }
            except Exception as e:  # noqa: BLE001
                print(f"  evidence unavailable for {case['name']}: {e}")
        problems = (
            validate_report(rep, sources) if ok else ["investigation did not complete"]
        )
        scores = judge(rep, judge_model, region) if ok else None
        rubric = rubric_average(scores)
        results.append(
            {
                "case": case["name"],
                "completed": ok,
                "policy_problems": problems,
                "rubric": rubric,
                "judge": scores,
                "cost_usd": inv.get("cost_usd"),
                "expected_hits": keyword_hits(json.dumps(rep), case.get("expect", [])),
            }
        )
    n = max(len(results), 1)
    rubrics = [r["rubric"] for r in results if r["rubric"] is not None]
    return {
        "completion": round(sum(r["completed"] for r in results) / n, 3),
        "policy_clean": round(
            sum(1 for r in results if not r["policy_problems"]) / n, 3
        ),
        "rubric_avg": (round(sum(rubrics) / len(rubrics), 2) if rubrics else None),
        "expected_hits": round(sum(r["expected_hits"] for r in results) / n, 3),
        "cases": results,
    }


# ---------------- gate, record, push ----------------


def push_cloudwatch(suite: str, scores: dict, region: str) -> None:
    """Eval scores as CloudWatch metrics (namespace Argus/Evals), next to the AgentCore
    Evaluations scores of live traffic, so the board and alarms see both."""
    import boto3

    data = [
        {
            "MetricName": k,
            "Dimensions": [{"Name": "suite", "Value": suite}],
            "Value": float(v),
            "Unit": "None",
        }
        for k, v in scores.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    if data:
        boto3.client("cloudwatch", region_name=region).put_metric_data(
            Namespace="Argus/Evals", MetricData=data[:1000]
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("EVAL_API_URL", "http://localhost:8000"))
    ap.add_argument("--suites", default="watch,investigator,tasking,report")
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--thresholds", default=str(HERE / "thresholds.yaml"))
    ap.add_argument(
        "--gate", action="store_true", help="exit 1 when a threshold is missed"
    )
    ap.add_argument(
        "--push", action="store_true", help="publish scores to CloudWatch (Argus/Evals)"
    )
    ap.add_argument(
        "--judge-model",
        default=os.getenv("EVAL_JUDGE_MODEL", "us.amazon.nova-pro-v1:0"),
    )
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    args = ap.parse_args()
    cases = yaml.safe_load(open(args.cases))
    thresholds = yaml.safe_load(open(args.thresholds))
    scenario = http().get(f"{args.api}/area", timeout=30).json().get("name")
    revision = os.getenv("GIT_SHA")
    out, failed = {}, []
    for suite in [s.strip() for s in args.suites.split(",") if s.strip()]:
        print(f"== {suite}")
        cfg = cases.get(suite, {})
        try:
            if suite == "watch":
                scores = suite_watch(args.api, cfg)
            elif suite == "investigator":
                scores = suite_investigator(cfg)
            elif suite == "tasking":
                scores = suite_tasking(cfg)
            elif suite == "report":
                scores = suite_report(args.api, cfg, args.judge_model, args.region)
            else:
                print(f"  unknown suite {suite}")
                continue
        except Exception as e:  # noqa: BLE001
            scores = {"error": str(e)}
        misses = check_thresholds(scores, thresholds.get(suite, {}))
        passed = not misses and "error" not in scores
        out[suite] = {"scores": scores, "misses": misses, "passed": passed}
        print(
            "  ",
            json.dumps({k: v for k, v in scores.items() if k != "cases"}, default=str),
        )
        if misses:
            print("  MISSED:", "; ".join(misses))
            failed.append(suite)
        try:
            http().post(
                f"{args.api}/evals",
                json={
                    "suite": suite,
                    "scenario": scenario,
                    "scores": {k: v for k, v in scores.items() if k != "cases"},
                    "passed": passed,
                    "thresholds": thresholds.get(suite, {}),
                    "code_revision": revision,
                    "details": {"cases": scores.get("cases", [])},
                },
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  could not record run: {e}", file=sys.stderr)
        if args.push:
            # gate_passed is what the CloudWatch alarm argus-eval-<suite> watches.
            push_cloudwatch(
                suite,
                {
                    **{k: v for k, v in scores.items() if isinstance(v, int | float)},
                    "gate_passed": 1.0 if passed else 0.0,
                },
                args.region,
            )
    (HERE / "results").mkdir(exist_ok=True)
    path = HERE / "results" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(
        json.dumps(
            {
                "api": args.api,
                "scenario": scenario,
                "code_revision": revision,
                "suites": out,
            },
            indent=1,
            default=str,
        )
    )
    print(f"results: {path}")
    if args.gate and failed:
        print(f"GATE FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
