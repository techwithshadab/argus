# Argus roadmap: from demo to product seed

Decisions behind this plan are in `docs/adr/` and the vocabulary in `CONTEXT.md`. Phases are ordered by dependency: each phase only needs what the previous ones built. Already in place: model provider switching (`MODEL_PROVIDER`), the Bedrock-only production guard, the isolated agent network tier, and the CDK lifecycle verbs.

## Phase 1: Identity, audit, review states

Foundations everything else records into.

- Done: one IAM role per agent (`argus-agent-*`); MCP servers and the API verify callers through STS-confirmed identity (`TOOL_AUTH=aws-iam`, no-op locally) and per-server role allowlists. ADR-0001.
- Done: `audit_events` append-only table (trigger blocks update/delete); every state change writes actor, time, details, trace id. `GET /audit`.
- Done: review state on alerts and investigations (`draft` -> `accepted` | `rejected`) with reviewer and time; tasking records who decided; UI shows AI drafts and review controls.
- Done: watch-officer identity comes from the load balancer's Cognito sign-in on AWS (`OFFICER_AUTH=oidc`, the signed `x-amzn-oidc-data` token verified by the API) and from a header locally. ADR-0018.
- Done: synth-time guard refuses any `modelProvider` other than bedrock and any non-first-party Bedrock model; agents run in an isolated subnet tier with no NAT route; IAM limits model invocation to the allowed vendors. ADR-0002, ADR-0007.
- Done: full lifecycle through CDK (`make deploy`, `update`, `stop-aws`, `start-aws`, `destroy`), X-Ray settings as custom resources, all log groups owned by CDK, bootstrap assets garbage-collected on destroy.

## Phase 2: Data model

- Done: entity and edge tables, `ownership_network()` SQL function, registry MCP tool `ownership_network(mmsi, depth)` with decrypted person names, `GET /network/{mmsi}` for the UI, Investigator prompt updated. ADR-0004.
- Done: day-partitioned positions (native partitioning, no primary key by design), retention policy as data, archiver service exporting expired days to Parquet (S3 on AWS via a daily scheduled task, a directory locally), evidence snapshots on every alert and completed investigation with `GET /evidence`. ADR-0005.
- Done: KMS key and Secrets Manager data key on AWS (`DATA_KEY` locally); `beneficial_owner` and person names stored encrypted with pgcrypto; archive bucket KMS-encrypted.

## Phase 3: Durable execution

- Done: `jobs` table plus Redis Streams (local) / SQS with DLQ (AWS); worker process with claim-by-update, exponential backoff on transient failures, per-job timeouts, dead status, audit events. `GET /jobs`. ADR-0008.
- Done: sweeps call the Watch agent directly over A2A from the worker; EventBridge Scheduler (AWS, `sweepIntervalMinutes`) and the worker's timer (local, `SWEEP_INTERVAL_MIN`) on one idempotency key; investigation policy (`AUTO_INVESTIGATE_SEVERITIES`) opens investigations for severe alerts.
- Done: node-level progress from the orchestrator's tool hooks (investigator, tasking, report) and the worker, on `/events` (SSE); the UI shows live steps, attempts and errors.

## Phase 4: Graph-owned orchestration and model tiers

- Done: investigation graph in code with parallel Investigator branches, pure merge, Tasking on the evidence gap, tool-less report node over validated JSON; progress per node. ADR-0003.
- Done: model tiers (fast: Watch; standard: Tasking; strong: Investigator, report) with `MODEL_TIER_<ROLE>` and `MODEL_ID_<TIER>` overrides; the Investigator escalates one tier when its output fails validation; the report node retries once with the policy violations.
- Done: safety layer: external free text marked and capped by the MCP servers (`untrusted`), every prompt says tool outputs are data, the report is checked for allowed actions and evidence traceability.
- Done: provenance manifest per investigation (code revision, prompt hashes, per-node models and attempts, MCP versions, schema version, evidence snapshot ids) stored and shown. ADR-0006.
- Open: the graph has been exercised only with the stub agents; the first real Bedrock run should be watched end to end.

## Phase 5: Evals and operations

- Done: deterministic detector evals on every PR against a PostGIS service in CI (`tests/integration`), over every scenario (a second scenario, `aegean_shadow`, added); per-node evals (`evals/node_evals.py`, cases and floors in YAML, LLM judge on Bedrock) nightly and on prompt/model/agent changes via `.github/workflows/evals.yml`, results in `eval_runs` and CloudWatch; `evals/e2e.sh` loops scenarios. ADR-0009.
- Done: SLIs computed by the API (`GET /slo`, Prometheus `GET /metrics`): sweep and investigation p95 latency, completion ratio, alert-to-investigation lag, cost per investigation from per-node token usage in the manifest, latest eval recall. Prometheus alert rules and a Grafana SLO dashboard locally; the ADOT collector scrapes the same endpoint into CloudWatch and CDK creates alarms with an SNS topic (`alertEmail`).
- Open: token usage is recorded for the Investigator branches and the report node; Watch and Tasking usage is not yet in the manifest, so the cost figure is a floor.

## Out of scope for now

Multi-tenancy, a dedicated graph database, TimescaleDB, Step Functions. Each has a named trigger in its ADR for when to revisit.
- Done: a queue and worker per kind with a timeout ladder (ADR-0016); Cognito sign-in, WAF and TLS on the watch floor (ADR-0018); CloudWatch alarms; password rotation; data re-keying; tier escalation on throttling; cdk-nag.
- Next: a LangGraph checkpointer for the Investigator (it needs a store the isolated agents may reach without a database route: AgentCore Memory or an S3 checkpointer through the existing endpoints) and an asynchronous hand-off so the worker does not hold a message for a whole investigation (ADR-0016); CloudFront in front of the balancer once a domain certificate exists (ADR-0018); a registry feed; a second availability zone for the agents, which means a new VPC because subnet CIDRs are allocated per zone in order and adding a zone would replace the existing subnets; the framework decision at the ADR-0017 review date.
