# Architecture: how do agents build a maritime case without ever deciding anything?

This document answers the question Argus exists to answer: *can a team of agents watch an area of sea, notice deception, build the case an analyst would want, and hand it to a person without any agent taking an action of its own?* It covers the end-to-end design, the decisions behind it (system design, data structures and algorithms, agent design, tooling), the constraints AWS imposed and how they were met, and what is verified rather than asserted. Vocabulary is fixed in `CONTEXT.md`; the reasoning behind each decision is in `docs/adr/`.

## The claim

Every alert and every Vessel of Interest (VOI) report is advisory, carries a review state, and is traceable to tool output. Anything with effect outside Argus is a **proposal** until a named watch officer approves it. That is enforced in the API, not in prompts: agents can only create `proposed` tasking rows, the human endpoints are separate, `audit_events` refuses updates and deletes, and `tests/test_graph_policy.py` pins the report policy that rejects any recommendation outside the allowed set or any indicator that cites no tool.

## End-to-end architecture

![How Argus works, as five blocks: the sources (positions, registries, sanction lists, maps, imagery, all treated as data); Argus in the account with Watch (five detectors, an agent raises alerts), Investigate (two lines of enquiry merged by code) and Propose (a Vessel of Interest report and a collection request); what every step carries (frozen evidence, a record of prompts, models and tools, guarded model calls, cents per case); the boundary where people decide; and the watch floor that reviews, approves and records every decision](docs/diagrams/architecture-overview.png)

<sub>Every figure shares one design system and regenerates from a committed source; the counts it states are checked against the repository in CI. See [docs/diagrams/README.md](docs/diagrams/README.md).</sub>

Read it left to right. Feeds arrive on the left and are treated as data, never instructions. The platform (column 01) is the system of record: PostGIS holds the picture, the MCP servers compute evidence from it, and the API enforces the human gate. The first red boundary is tool authorization: every tool call goes through the AgentCore Gateway, where a Cedar policy per agent role decides, and an agent-only API route answers only a caller whose IAM role STS confirms. The agents (column 02) reason inside a sweep lane and an investigation lane, and the invariant they defend is printed on the column. The second boundary is the human gate: only proposals cross it. The watch floor (column 03) is where people decide, and the governance plane underneath is what makes every decision reproducible and observable.

## Principles

1. **Agents advise, humans act.** Findings carry a review state; actions carry an approval. The two are never conflated (`CONTEXT.md`).
2. **Code owns the workflow; models own the judgement.** The investigation is a graph in code with LLM judgement inside nodes. Nothing a model says can reorder the graph or skip a step (ADR-0003).
3. **Deterministic evidence.** Detectors are SQL over PostGIS; agents reason over tool output and must cite it (`shared/policy.py`).
4. **Data stays in the account.** Model traffic goes to Bedrock through a VPC endpoint; agents run in isolated subnets with no internet route; only Amazon Nova models are grantable (ADR-0002, ADR-0007).
5. **Reproducible findings.** Evidence snapshots and a provenance manifest per investigation: code revision, prompt hashes, model and tier per node, tool versions, attempts (ADR-0005, ADR-0006).
6. **Durable, observable work.** Sweeps and investigations are jobs with retries, timeouts and a dead-letter path; SLIs come from the same tables (ADR-0008).

## Decision log

**System design.** One internal deployment per organisation and AWS account, as the seed of a real maritime domain awareness product rather than a multi-tenant service (`docs/adr/` records each trade-off). The platform runs on ECS Fargate with Aurora Serverless v2 PostGIS; the four agents run as Bedrock AgentCore runtimes so that each has its own IAM identity, its own log group and its own network position. Both the local compose stack and the AWS deployment are first-class and share every image.

**Data structures and algorithms.** `positions` is range-partitioned by day and has no primary key, because AIS carries duplicates and a spoofed identity is two transmitters sharing one MMSI; the detectors (gap, conflict, loitering, rendezvous, incursion) are window functions and spatial joins over it. The ownership network is a property graph in Postgres (`entities`, `edges`) walked by a recursive CTE that returns the k-hop neighbourhood and the shortest path to every sanction listing; that is the Graph RAG the Investigator uses. Merging the two Investigator branches is a pure function (owning branch wins each field, lists union, confidence takes the lower value) so it can be unit-tested without a model.

**Agent design.** Watch (Strands, fast tier) judges detector candidates; Investigator (LangGraph, strong tier) runs twice in parallel with different scopes; Tasking (Strands, standard tier) decides whether collection would reduce uncertainty; the Orchestrator is a code graph whose only model call is the report node. Model tiers map to roles and escalate one step when a node's output fails validation or the tier's model is throttled or unavailable after the SDK's own retries (a different model, its own quota; the manifest records `escalated_to`). Free text from any external source passes through `common/safety.untrusted()` before a model sees it.

**Tools.** MCP over streamable HTTP for every tool, so the same servers serve Strands and LangGraph agents and carry a version into every span and manifest. A2A between the orchestrator and the specialists. OpenTelemetry everywhere, with CloudWatch GenAI Observability optional locally. CDK for the whole AWS lifecycle, with a synth-time guard that refuses any model provider except Bedrock and any Bedrock vendor except Amazon.

## The investigation graph

![Argus investigation graph: three triggers into one durable job; the orchestrator as a graph in code with two Investigator branches in parallel, a pure merge, the Tasking agent, the report writer and its policy and schema check with one correction loop, persistence of report, snapshots and manifest, and vessel memory feeding the next run; then the watch floor where officers review the draft, approve the proposal, and every decision lands in the audit log](docs/diagrams/investigation-graph.png)

The graph has five numbered steps and two boundaries. A trigger (the severity policy on a new alert, a watch officer, or an eval) creates a `jobs` row under an idempotency key, so a second request joins the running case. The worker invokes the orchestrator runtime with its own role and streams node progress to the UI. Steps 01a and 01b run in parallel on the Investigator runtime with different scopes; step 02 merges them deterministically; step 03 hands the extracted evidence gap to Tasking, which may create a `proposed` request; step 04 writes the report from the merged JSON alone and checks it against the policy, retrying once with the violations; step 05 persists the report, the evidence snapshots and the manifest. Only then does anything reach the watch floor, and only as a draft.

## Stages in detail

### 1. Ingest and replay (`services/ais-replay/replay.py`, `data/synthetic/generator.py`)

On every start the replay task applies every `data/sql/*.sql` except `000_*` (idempotent, so Aurora and a fresh local volume converge), loads reference data, encrypts personal data with the data key, builds the ownership network, ensures the daily partitions, bulk-loads the scenario, publishes the area and ground truth to `scenario_meta`, then streams positions on a loop so the floor never goes quiet. Live mode subscribes AISStream to the same bounding box and injects nothing.

### 2. Evidence tools (`mcp-servers/servers/*.py`, `mcp-servers/common/`)

Four servers, twenty-four tools, all deterministic SQL over PostGIS except the three that call an external feed (OpenSanctions, OpenStreetMap, the Sentinel catalogue), whose free text is marked untrusted and capped. Each server verifies the caller's STS-signed token against its role allowlist (`callerauth.py`), reports its version on `/health` and in every span, and decrypts personal data in exactly one tool.

### 3. The sweep (`agents/watch/app.py`, `agents/shared/prompts/watch.md`)

EventBridge Scheduler (AWS) or the worker's timer (local) queues a sweep on an interval-bucket key. The Watch agent's first tool call, `list_candidates`, runs the five SQL detectors and the open-alert list in code (`agents/shared/sweep.py`) and returns candidates with fixed vessel ids, kinds, time windows and detector evidence, deduplicated against what is already open. The model then gathers context per candidate and disposes of each one: `raise_alert(candidate_id, severity, score, rationale)` or `dismiss_candidate(candidate_id, reason)`. Vessel, kind and window never come from the model (a small model copied a gap window from one hull to another before this split), and the API snapshots the evidence and may open an investigation by severity policy.

### 4. The investigation (`agents/orchestrator/app.py`, `agents/shared/graph.py`, `agents/investigator/app.py`, `agents/tasking/app.py`)

The graph above. The orchestrator is the only runtime allowed to invoke the specialists and to use the vessel memory; the specialists speak A2A and expose `/provenance` so the manifest can record their model, tier and prompt hash.

### 5. The report and the policy (`agents/shared/policy.py`, `agents/shared/prompts/report.md`)

A tool-less model call over the merged findings produces the VOI report: headline, priority, confidence, timeline, indicators and counter-indicators, ownership and sanctions, recommended actions, collection plan, evidence, gaps, caveats. The policy check accepts only allowed action patterns and only indicators whose source a tool produced; a second violation fails the investigation closed.

### 6. Review and approval (`services/api/main.py`, `services/ui/index.html`)

Alerts and reports are reviewed (accept or reject, Shift-key shortcuts so stray typing cannot decide anything); tasking requests are approved or rejected. Every decision records who and when and appends an audit event. The officer's identity is a request header until the load balancer gets an OIDC login.

### 7. Provenance and evidence (`agents/shared/provenance.py`, `data/sql/004_*.sql`, `data/sql/006_*.sql`)

`evidence_snapshots` freezes what each finding cited; `investigations.manifest` records how it was produced; `GET /audit` says who acted. Together they answer, months later, "why did Argus say this, with what, and who agreed".

## Data model

![Argus data model on PostGIS, three concerns left to right: what the feeds said (positions, zones, scenario metadata, registry, entities and edges, sanctions), what Argus concluded (alerts, jobs, investigations, tasking requests, evidence snapshots, eval runs) with the referencing columns highlighted, and the record (append-only audit events, retention classes, the idempotent schema)](docs/diagrams/data-model.png)

Three concerns, left to right: what the feeds said, what Argus concluded, and the record of who did what. Personal data (`registry.beneficial_owner`, person entities) exists only as pgcrypto ciphertext; the key comes from `DATA_KEY` locally and a KMS-encrypted secret on AWS. `jobs` is the source of truth for all agent work; the queue carries only ids. Schema changes are numbered idempotent files under `data/sql/`.

## AWS deployment

![Argus technical architecture on AWS with official service icons: the watch floor and the external feeds outside; the edge (WAF, Cognito), jobs and scheduling, deployment, and identity, secrets and configuration along the top; the VPC with its public, application, data, self-hosted monitoring and isolated agent subnets, the four AgentCore runtimes and the four MCP tool runtimes; the AgentCore services (two gateways, registry, identity, memory, evaluations, harness and configuration bundle pilots), Bedrock, and the AWS monitoring column (AgentCore Observability, Transaction Search, CloudWatch, X-Ray, SNS, operator email), and the calls between them](docs/diagrams/technical-architecture.png)

Four CDK stacks in order: `argus-network`, `argus-data`, `argus-platform`, `argus-agents`. Agents sit in isolated subnets with no NAT route; everything they need (Bedrock, AgentCore and its Gateway, the Agent Registry, STS, logs, ECR and its S3 layers, secrets, SSM, X-Ray) is an interface or gateway endpoint; tools are reached only through the AgentCore Gateway and the API only through the internal load balancer. The four MCP servers run as AgentCore Runtime endpoints in the private subnets, invocable by the gateway's role alone. AgentCore restricts VPC mode to certain zones by zone id, so the deploy script resolves them per account and the runtimes use only matching agent subnets. Agents find each other through the AWS Agent Registry (records first, environment as the fallback) and call each other through a second gateway, `argus-agents`, with the specialist runtimes as its targets, so every A2A call is logged in one place; the worker reaches Watch the same way. The OpenSanctions key is an AgentCore Identity credential provider: the registry server exchanges each invocation's workload access token for the key instead of reading a secret. A Tasking harness (the managed agent loop, with its own Cedar policy) and a configuration bundle for the report node are wired behind flags as pilots (ADR-0013, ADR-0014). The public load balancer is HTTPS only, signs officers in with Cognito on every listener and sits behind AWS WAF; the internal load balancer's API listener is HTTPS with a deploy-time certificate agents trust through SSM (ADR-0018). Jobs run on a queue per kind with their own worker services and one timeout ladder (ADR-0016); the database password rotates monthly and every consumer re-reads it on failure. Thirty-one CloudWatch alarms (in the default configuration) watch the layer under the Grafana rules and page the same SNS topic; cdk-nag runs on every synth with its suppressions and their evidence in `infra/cdk/stacks/nag.py`. Lifecycle is entirely IaC: `make deploy`, `make stop-aws`, `make start-aws`, `make destroy`; see `docs/RUNBOOK.md`.

## Trust boundaries

- **Tool authorization and caller identity.** Each agent runtime has its own least-privilege IAM role. Tool calls pass the AgentCore Gateway, whose Policy engine evaluates one Cedar policy per role on every call (default deny, forbid wins, decisions logged; ADR-0011). Agents find each other through the AWS Agent Registry and call each other through a second gateway; the OpenSanctions key comes from AgentCore Identity's token vault; a Harness and a configuration bundle are wired as pilots behind flags (ADR-0013). Agent-only API routes accept a caller only if STS confirms its role, and fail closed (ADR-0001).
- **Model traffic.** Never leaves the account; only Amazon Nova models are grantable, and synth refuses anything else (ADR-0002).
- **Prompt injection.** External free text is marked and capped; every prompt states tool output is data; reports are checked for allowed actions and evidence traceability.
- **The human gate.** Proposals only cross into the watch floor. On AWS the officer is whoever Cognito signed in at the load balancer: the API verifies the balancer's signed id token and records the email on every review, approval and sweep; scripts and evals sign with IAM through the `argus-operator` role (ADR-0018). Locally the officer id is a typed header.

## Observability

One OTLP exporter per process. Locally: OTel collector, Tempo, Loki, Prometheus, Grafana (one board: a KPI strip, the watch floor, then collapsed sections for pipeline health, agents and tools, one investigation, models and cost, and on AWS AgentCore and Bedrock; alert rules route to the SNS topic), optional CloudWatch GenAI Observability. On AWS: the platform collector exports only into the same Grafana, Tempo, Loki and Prometheus images running as one Fargate task (Grafana state on Aurora, Tempo and Loki on EFS), so the AWS Grafana shows the same dashboards as the local one; the agents' own telemetry lands in CloudWatch (AgentCore Observability, Evaluations, Policy; Transaction Search on by default, ADR-0012) with a second span exporter to that collector for the Grafana view; prompts are versioned in Bedrock Prompt Management and the officer's verdict is a CloudWatch metric (ADR-0012). Strands emits `gen_ai.*` spans; LangGraph uses OpenInference; MCP servers add `mcp.tool.*` and `mcp.server.version`. The API computes SLIs (`/slo`, `/metrics`): sweep and investigation p95 latency, completion ratio, alert-to-investigation lag, cost per investigation from manifest token usage, latest eval recall. The Grafana rules page on SLO burn, cost, the eval gate and the loss of the API scrape; CloudWatch alarms (`infra/cdk/stacks/alarms.py`) page on the layer beneath them, which stays visible when the API is down: queues and dead letters, balancers and targets, running tasks, Aurora, NAT, WAF, degraded Investigator branches and the eval gate as pushed by the evals.

## Evaluation

Three layers (ADR-0009): deterministic detector evals on every pull request against PostGIS in CI (every injected anomaly in every scenario must be found); per-node evals with floors (Watch recall and precision, Investigator schema and traceability, Tasking decision, report policy and an LLM-judge rubric) nightly and on prompt or model changes; end-to-end runs per scenario. Results feed the eval-recall SLO. A prompt or model change is not done until `evals/node_evals.py --gate` passes.

## Models

A Bedrock Guardrail (`argus-agents`) sits on every model call in both frameworks: harmful-content filters, one denied topic (weapons targeting) and profanity. Three calibration choices shape it. There is no denied topic for surveillance of a named private person: the topic classifier blocks about one live Investigator prompt in five with such a topic ("retrieve the vessel's track ... who controls it") even with a vessel carve-out in its definition, so personal data is protected by encryption and the registry tool instead. The misconduct filter runs low on input and off on output, because describing suspected smuggling or sanctions evasion is the product. The prompt-attack filter runs low, because Bedrock applies it to the prompt as a whole, and every prompt here is machine-built; tool text, the real injection surface, reaches the model as tool results that the filter does not read, so it is marked untrusted and capped by `common/safety.untrusted()` and the report policy instead. With the prompt-attack and misconduct filters at high, the guardrail blocks about one model call in thirty on ordinary investigations, and every block costs a branch or a retry. No PII masking, because reports must name owners and listed persons. AgentCore Memory is read at the start of every investigation and handed to both Investigator branches as prior context, capped and marked as unverified; the report node runs as a plain metered call so its tokens count. Amazon Nova on Bedrock only, by tier: Nova Lite (fast: Watch sweeps), Nova 2 Lite (standard: Tasking), Nova Pro (strong: Investigator branches and the report node), Nova Premier as the step-up. One-step escalation when a node's output fails validation. Other providers exist for development and eval comparison only and are refused by the AWS deployment.

## Key invariants

- Agents create `proposed` rows only; approve and reject are human endpoints.
- Findings carry `review_state`; actions carry an approval; never the same field.
- Every API state change appends an audit event; the table refuses updates and deletes.
- Every indicator in a report cites a source a tool produced.
- Every investigation stores its manifest and snapshots before it is shown.
- Nothing in AWS is created outside CDK.

## Honest limitations

- Without a domain certificate the watch floor's HTTPS certificate is self-signed (one browser warning per profile); CloudFront in front of the balancer waits for a domain.
- Live AIS mode has no injected anomalies, so the evals mean nothing there; the demo runs on the synthetic east Mediterranean scenario.
- Agents run in a single availability zone in us-east-1 with the default two-zone VPC, because AgentCore supports only some zones.
- Deploys run as the `argus-deployer` role (administrator, MFA only); the IAM user or Identity Center principal that assumes it is created outside the app, and the account's first bootstrap needs credentials that can create it.
- The database password rotates on a schedule; the data key is re-keyed by an operator (`make rekey-aws` rewrites every encrypted row, then stores the key).
- A worker restart loses the in-flight graph state; the job is reclaimed and restarted from the beginning (a checkpointer is the next step, ADR-0016).

## Where to go deeper

`docs/TECHNICAL.md` (components, configuration, job lifecycle), `docs/API.md` (HTTP and MCP reference), `docs/USE_CASES.md`, `docs/RUNBOOK.md`, `docs/SECURITY.md`, `docs/ROADMAP.md`, and `docs/adr/` for why.
