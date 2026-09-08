# ADR-0012: Native agent observability; Langfuse removed

Date: 2026-09-07. Status: accepted.

## Context

Langfuse was the LLM-centric trace browser: sessions, prompt versions, datasets and the
watch officer's verdict as a score. After ADR-0011 the agents emit unified telemetry to
CloudWatch, AgentCore Evaluations scores live sessions, and the same account offers
managed prompts and datasets. Langfuse had become a second copy of that, with its own
Fargate task, ClickHouse, Redis and EFS to keep alive and a fourth console to secure.

## Decision

- **Traces and sessions**: AgentCore Observability in CloudWatch (unified telemetry per
  runtime log group, Transaction Search on by default because the GenAI Observability
  views and Evaluations index spans through it, `-c transactionSearch=false` to skip).
  Grafana stays the operator board and links into those views.
- **The platform collector exports only to the self-hosted Tempo, Prometheus and Loki**
  and carries no AWS permissions; the agents add one second span exporter to that
  collector (`ARGUS_OTLP_ENDPOINT`) so the Grafana board covers them too, which is the
  only intended duplicate. Under `AGENT_OBSERVABILITY_ENABLED` the frameworks instrument
  into the ADOT-installed tracer provider rather than building their own.
- **Prompt versions**: Bedrock Prompt Management. The deploy publishes every
  `agents/shared/prompts/*.md` as a managed prompt and cuts an immutable version whenever
  the text changes (`infra/cdk/stacks/prompts.py`). Runtimes load the version by ARN and
  the manifest records the version number beside the content hash; the repository file
  remains the source of truth and the fallback.
- **Officer verdicts**: the API publishes accept/reject as CloudWatch metrics
  (`Argus/Reviews`) next to the evaluation scores; the database and audit table remain
  the record.
- **Datasets and experiments**: `evals/agentcore_dataset.py` turns the eval cases into an
  AgentCore dataset of predefined scenarios (inputs, expected tool trajectory, assertions)
  and starts batch evaluations over any time window of live sessions for pre/post
  comparison. `node_evals.py --gate` stays the CI gate; `--push` publishes its scores to
  CloudWatch (`Argus/Evals`).
- Langfuse is removed from the platform stack, the collector, the API, compose and the
  Makefile.

## Consequences

- One fewer console and Fargate task; roughly sixty dollars a month less plus storage.
- Prompt lineage is auditable in the Bedrock console and immutable per version.
- AgentCore Evaluations and datasets are preview features: their scores are signals, not
  gates, until AWS marks them generally available.
