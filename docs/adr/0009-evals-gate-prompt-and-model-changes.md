---
status: accepted
---
# Evals are layered and gate prompt and model changes

Before phase 5 the only measurement was a hand-run script scoring Watch alerts against ground truth. Decision: three layers, each with a different cost and trigger. Deterministic detector evals run on every pull request against a real PostGIS service in CI and involve no model: every injected anomaly in every scenario must be found by its detector, and raw false positives are bounded. Per-node evals (Watch recall and precision, Investigator schema validity, evidence traceability and expected content, Tasking decision match, report completion, policy cleanliness and an LLM-judge rubric) run against a live stack nightly and whenever prompts, model configuration or agent code change, with floors in `evals/thresholds.yaml`; a missed floor fails the gate. End-to-end runs iterate over every scenario. Results are recorded in `eval_runs` so the eval-recall SLO reads the latest run, and pushed to CloudWatch next to traces.

## Considered options

- End-to-end only: cheaper, but a regression cannot be attributed to a node, and it needs a model for everything.
- Manual runs: prompt and model changes shipped unmeasured, which is how they had been shipping.

## Consequences

The model-in-the-loop layer needs a running stack and credentials, so the workflow skips with a notice when `EVAL_API_URL` is not configured; it must never block contributors without a stack. An unmeasured score (judge unavailable) is reported, never failed. Adding a scenario adds detector coverage for free; adding a node case adds a floor that a prompt change has to keep.
