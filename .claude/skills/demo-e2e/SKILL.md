---
name: demo-e2e
description: Bring up the local compose stack, wait for agents to be healthy, run a watch sweep, and score alerts against ground truth with make eval. Use to verify agent, prompt, or MCP changes end to end.
disable-model-invocation: true
---

Arguments: `$ARGUMENTS` (optional; `keep` skips the teardown reminder).

Prerequisites to check first, and stop with a clear message if any is missing:
- `.env` exists (else tell the user to `cp .env.example .env` and set `MODEL_PROVIDER` plus its credential).
- `docker info` succeeds.
- The credential for `MODEL_PROVIDER` is present: `~/.aws` has the profile named in `.env` for `bedrock`, or the matching `*_API_KEY` is set for `anthropic` / `openai` / `gemini`.
- Container logs mentioning `needs ... in the environment` mean the provider key is missing.

Steps, from the repo root:

1. Start the stack: `make up`. Builds can take several minutes on first run.
2. Wait for readiness. Poll until all of these return 200, giving up after 5 minutes:
   - `http://localhost:8000/health` (API)
   - `http://localhost:8001/health` … `8004/health` (four MCP servers)
   - `http://localhost:9001/ping`, `9002/ping`, `9003/ping` (watch, investigator, tasking)
   - `http://localhost:8080/ping` (orchestrator)
   If an agent container never becomes ready, run `docker compose logs --tail 50 <service>` and look for `MCP servers not reachable` (an MCP server is unhealthy) or Bedrock credential errors before anything else.
3. Wait for ground truth: poll `http://localhost:8000/ground-truth` until it returns a non-empty JSON array (ais-replay writes it after loading the scenario).
4. Trigger the sweep: `make sweep`. Then wait ~90 seconds (the latency budget in `scripts/demo.sh`), polling `GET /alerts` every 15 s and stopping early once the alert count stops growing for two consecutive polls.
5. Score: `make eval` (needs `httpx`; if missing, `uv run --with httpx==0.28.1 python evals/run_eval.py --api http://localhost:8000`). Report recall, precision, and `recall_by_kind`.
6. Optionally run `make investigate` and fetch the resulting investigation from `GET /investigations` to show a Vessel of Interest report.

Finish with a short summary: what was up, alert count, eval scores, and any container that logged errors. Remind the user that `make down` deletes all volumes and that `docker compose stop` preserves them, unless `keep` was passed.
