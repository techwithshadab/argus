# Evaluation

`run_eval.py` scores the Watch Agent against the injected anomalies in the scenario (ground truth
is written by the replay service and never exposed to the agents through MCP).

- Recall per anomaly kind: did an alert of that kind exist for that MMSI with overlapping time?
- Precision: how many raised alerts match a ground-truth anomaly.
- Optional: `--push` publishes the scores as CloudWatch metrics (Argus/Evals) next to the AgentCore Evaluations scores, so the
  numbers sit next to the traces that produced them.

Run against a live stack:

    make eval          # or: python evals/run_eval.py --api http://localhost:8000

`inspect_task.py` wraps the same check as an Inspect AI task so it can run in CI with other evals.
