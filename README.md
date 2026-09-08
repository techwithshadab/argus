# Argus: Dark Vessel Investigation

Argus, the hundred-eyed watchman, is a multi-agent maritime domain awareness demo on Amazon Bedrock AgentCore.

It watches AIS traffic, spots vessels behaving deceptively (going dark near a
subsea cable, spoofing an identity, loitering in an exclusion area, meeting another ship at sea),
investigates them against registry and sanctions data, proposes satellite re-look tasking for a human
to approve, and produces a Vessel of Interest report with every claim traced to evidence.

All vessels, owners, sanctions and zones are fictional. Real, free data sources (AISStream.io,
Copernicus Data Space, OpenStreetMap, OpenSanctions) can be switched on with environment variables.

## What is in the box

| Layer | Choice | Why |
|---|---|---|
| Agents | Strands Agents SDK 1.54 (watch, tasking, orchestrator) and LangGraph 1.2 + LangChain 1.3 (investigator) | Shows that A2A and MCP make the framework a per-agent decision |
| Agent hosting | Amazon Bedrock AgentCore: nine Runtime endpoints (four agents, four MCP servers, the tasking harness pilot), two Gateways (tools with a Cedar policy engine, agents for A2A), Agent Registry, Identity, Memory, online Evaluations, a Bedrock Guardrail and managed prompts | Managed, session-isolated, IAM-native; VPC mode keeps the agents in subnets with no internet route |
| Tools | 4 MCP servers over streamable HTTP (official `mcp` SDK 1.29): `ais`, `registry`, `geo`, `imagery` | Deterministic evidence tools; agents reason, they do not compute anomalies themselves |
| Agent to agent | A2A protocol (`a2a-sdk` 0.3.x via Strands; native `a2a-sdk` server for the LangGraph agent), SigV4-signed on AWS | Orchestrator discovers specialists from their agent cards |
| Models | Amazon Bedrock with Amazon Nova by tier (Lite, 2 Lite, Pro) in production; Anthropic API, OpenAI and Google Gemini for development and eval comparison | `MODEL_PROVIDER` + `MODEL_ID`; see [Model providers](#model-providers) |
| Data | PostGIS 17, Redis stream, synthetic AIS replay with injected anomalies (ground truth kept for evals) | Same schema locally (Postgres container) and on AWS (Aurora Serverless v2) |
| Observability | OpenTelemetry everywhere: Strands GenAI spans, OpenInference for LangGraph, MCP tool spans, FastAPI/httpx/psycopg auto-instrumentation. Local and AWS alike: a collector into Grafana, Tempo (span metrics and service graph), Prometheus and Loki with one generated board; on AWS also CloudWatch with Transaction Search, X-Ray, 31 alarms on an SNS topic and AgentCore's own unified telemetry | One exporter, both backends |
| Evaluation | Ground-truth recall/precision scorer, per-node suites with regression floors (`evals/node_evals.py --gate`), an Inspect AI task, AgentCore online Evaluations with a report rubric | Agents never see the ground truth; a prompt or model change is not done until the gate passes |
| Infra | Docker Compose locally; AWS CDK (Python) with 4 stacks | One command either way |

![How Argus works, as five blocks: the sources (positions, registries, sanction lists, maps, imagery, all treated as data); Argus in the account with Watch (five detectors, an agent raises alerts), Investigate (two lines of enquiry merged by code) and Propose (a Vessel of Interest report and a collection request); what every step carries (frozen evidence, a record of prompts, models and tools, guarded model calls, cents per case); the boundary where people decide; and the watch floor that reviews, approves and records every decision](docs/diagrams/architecture-overview.png)

## Technical architecture on AWS

![Argus on AWS: officers and operators arrive through a public load balancer with Cognito sign-in and AWS WAF; the platform stack runs the API, two job workers, the AIS ingest task, the OpenTelemetry collector and the Grafana task on ECS Fargate with Aurora PostGIS, SQS and EventBridge Scheduler; the agents plane runs the four agents and the four MCP tool servers on Bedrock AgentCore behind two gateways with a policy engine, the Agent Registry, Identity, Memory and Evaluations, in isolated subnets that reach AWS only through interface endpoints; Amazon Nova on Bedrock with a guardrail; CloudWatch and X-Ray beside the self-hosted Grafana, Tempo, Loki and Prometheus](docs/diagrams/technical-architecture.png)

Left to right: the edge (Cognito on every listener, WAF, HTTPS only), the platform (API, workers, ingest,
collector, Grafana task, Aurora, queues, scheduler), the agents plane on Bedrock AgentCore (four agents,
four MCP servers, two gateways with Cedar policies, registry, identity, memory, evaluations, guardrail,
managed prompts) in subnets with no internet route, and the two observability paths. The narrative
version with code paths per stage is [ARCHITECTURE.md](ARCHITECTURE.md); the figure sources are in
[docs/diagrams/](docs/diagrams/README.md).

Full documentation: [docs/README.md](docs/README.md) (architecture with diagrams, technical reference, API, use cases, runbook, security, ADRs). The figures regenerate from committed sources: [docs/diagrams/README.md](docs/diagrams/README.md).

## Quick start (local)

Prerequisites: Docker Desktop (or compatible) and credentials for one model provider. The default
is Amazon Bedrock, so AWS credentials with Claude model access (agents call Bedrock even when
running locally); set `MODEL_PROVIDER` to use another provider instead (see [Model providers](#model-providers)).

    cp .env.example .env            # set MODEL_PROVIDER + its credential, optionally an AISSTREAM_API_KEY
    make up                          # builds and starts ~15 containers
    open http://localhost:8088       # watch floor UI

Then either click **Run watch sweep** in the UI, or run the scripted path:

    ./scripts/demo.sh

What you should see: the replay animates 8 vessels south of Cyprus. The watch sweep raises alerts for
MERIDIAN STAR (rendezvous, then a 60-minute AIS gap ending inside Subsea Cable Corridor Kappa),
LIBERTY GRACE (one MMSI, two tracks), OCEAN PILGRIM (loitering inside the naval exercise area) and
NAVAND 3 (rendezvous). Click **Investigate** on MERIDIAN STAR: the orchestrator calls the Investigator
(LangGraph) and Tasking (Strands) agents over A2A, a SAR re-look request appears under "Collection
requests awaiting approval", and a Vessel of Interest report renders with timeline, indicators,
counter-indicators, evidence and information gaps.

Endpoints:

| Service | URL |
|---|---|
| UI | http://localhost:8088 |
| API (OpenAPI) | http://localhost:8000/docs |
| Grafana (admin, password `GRAFANA_ADMIN_PASSWORD`), board `argus` | http://localhost:3000 |
| Tempo, Prometheus, Loki | :3200, :9090, :3100 |
| MCP servers (health at `/health`, MCP at `/mcp`) | :8001 ais, :8002 registry, :8003 geo, :8004 imagery |
| A2A agent cards | http://localhost:9001/.well-known/agent-card.json (watch), :9002 investigator, :9003 tasking |
| Orchestrator (AgentCore HTTP contract) | http://localhost:8080/invocations, /ping |

Evaluate the watch agent against the injected anomalies:

    make eval

## Deploy to AWS

Prerequisites: AWS CLI, Node 20+, Python 3.12, Docker with buildx (AgentCore images are arm64, the
certificate issuer bundles on amd64), Bedrock model access for Amazon Nova enabled in the target
region, and a region where Bedrock AgentCore is available. Put `OFFICER_EMAIL` in `.env` so the
first watch-floor account exists after the deploy.

    make deploy

`scripts/deploy.sh` bootstraps CDK and deploys four stacks:

1. `argus-network`: VPC with public, private (NAT per zone) and isolated agent subnets, security groups, interface endpoints for every AWS service the agents use, flow logs, and the `argus-deployer` role (administrator, MFA only) for every deploy after the first.
2. `argus-data`: Aurora Serverless v2 PostgreSQL 17 (PostGIS created by the schema script, IAM authentication, seven-day backups, monthly password rotation), ElastiCache Serverless (Valkey), the data-key secret, the S3 archive and an access-log bucket.
3. `argus-platform`: ECS Fargate services for the API, the investigation and sweep workers, the UI, AIS ingest, the OpenTelemetry collector and the self-hosted Grafana task; two SQS queues with dead letters and an EventBridge schedule; an internal ALB (HTTPS to the API) for the agents; a public ALB (HTTPS only, Cognito sign-in on every listener, AWS WAF) for the watch floor; the `argus-operator` role for tooling; 31 CloudWatch alarms on one SNS topic.
4. `argus-agents`: the four agent runtimes and the tool plane on AgentCore: the four MCP servers as AgentCore Runtime endpoints, a gateway with a Cedar policy engine in front of them, a second gateway in front of the specialist agents, the Agent Registry records, Identity credential provider, Memory, online Evaluations, a Bedrock Guardrail, managed prompts, and the Harness and configuration-bundle pilots.

The API's workers invoke the orchestrator with `InvokeAgentRuntime`; the orchestrator finds the
specialists in the Agent Registry and calls them through the agents gateway with SigV4. Container
images and environment variable names are identical to the compose stack. Everything is created by
CDK and removed by `make destroy`; cdk-nag runs on every synth.

After the deploy, `docs/ACCESS.md` says where everything is and how officers sign in (Cognito
hosted page, temporary password by email, optional or mandatory MFA). Tooling reaches `/api/*`
with a caller token instead of the browser sign-in: `make eval-aws` assumes the `argus-operator`
role and runs the node evals against the deployment.

## Repository layout

    agents/                 shared/ (config, schemas, prompts, telemetry, MCP + A2A helpers), watch/, investigator/, tasking/, orchestrator/
    mcp-servers/            one image, four servers (servers/ais.py, registry.py, geo.py, imagery.py), common/ (db, telemetry)
    services/               ais-replay (synthetic generator + live AISStream), api (FastAPI), ui (MapLibre)
    data/                   sql/ schema, scenarios/ YAML, synthetic/ generator
    observability/          collector, Tempo, Loki, Prometheus, Grafana provisioning + dashboard
    infra/cdk/              CDK app and stacks
    evals/, tests/          scoring harness, Inspect task, unit tests
    scripts/                deploy.sh, demo.sh

## Design notes (agentic best practices applied)

- **Evidence tools, reasoning agents.** Anomaly detection is deterministic SQL in the AIS MCP server. The model decides what matters and why; it cannot invent a gap or a rendezvous.
- **Structured contracts.** Pydantic schemas for alerts, findings, tasking recommendations and the final report. The investigator's output is validated before it crosses the A2A boundary; the orchestrator returns a structured `VesselOfInterestReport` and persistence happens in code, not by a model remembering to call a tool.
- **Human on the loop.** Agents raise alerts and *propose* tasking. Approving a collection request or dismissing an alert is a human action in the UI and the API.
- **Prompts as files.** `agents/shared/prompts/*.md` carry the tradecraft (separate observation from inference, list counter-indicators, cite tools, state confidence). Change the tradecraft without touching code.
- **Protocol boundaries.** MCP for tools, A2A for agents. The LangGraph investigator and Strands orchestrator never import each other.
- **One trace per investigation.** UI shows the trace id on the report; follow it in Grafana/Tempo (service graph shows orchestrator -> A2A peers -> MCP servers -> Postgres) or CloudWatch GenAI Observability (token usage, prompts, tool I/O).
- **Same artefacts, two targets.** Compose and CDK use the same images, ports and env names.
- **Memory with a purpose.** AgentCore Memory keeps prior assessments per vessel, so a repeat offender's history is surfaced on the next investigation.

## Configuration

See `.env.example`. Notable switches: `AIS_MODE=live` with `AISSTREAM_API_KEY` for real AIS in the
scenario bounding box plus the `data/areas.yaml` regions named by `WATCH_AREAS` (`all` by default;
the UI's "watching" selector filters the map per area in both modes); `GEO_USE_OSM=true` for Overpass/Nominatim; `OPENSANCTIONS_API_KEY` for real
sanctions screening; `BEDROCK_GUARDRAIL_ID` to apply a Bedrock Guardrail on every model call;

## Known limits and things to verify in your account

- Without `uiCertificateArn` and `uiDomain` the watch floor's certificate is self-signed: one browser warning per profile, then an encrypted, signed-in session. CloudFront in front of the balancer needs that domain first.
- Bedrock AgentCore supports VPC mode in some availability zones only (`agentcoreZoneIds` in `cdk.json`); with the default two-zone VPC the agents run in one zone.
- Live AIS mode has no injected anomalies, so the evals mean nothing there; use the synthetic scenario for scoring.
- The imagery server searches the Copernicus catalogue anonymously; downloads and real tasking are out of scope.
- Costs: Aurora Serverless v2, two NAT gateways, two ALBs, ElastiCache Serverless, WAF and Fargate tasks run continuously (about $400 a month idle with the consoles, $140 stopped). `make stop-aws` between demos, `make destroy` when done.

## Model providers

Every agent gets its model from `agents/shared/models.py`, selected by two variables:

| `MODEL_PROVIDER` | Credential | Default `MODEL_ID` | Notes |
|---|---|---|---|
| `bedrock` (default) | AWS credential chain (`AWS_PROFILE` or access keys) | tiers: Nova Lite / Nova 2 Lite / Nova Pro | Amazon Nova only (no Claude); `MODEL_ID` pins one model for every tier; Nova Premier as the step-up for `strong` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5` | Claude 5 models run adaptive thinking and ignore `MODEL_TEMPERATURE` |
| `openai` | `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`) | `gpt-5` | Any OpenAI-compatible endpoint via `OPENAI_BASE_URL`; reasoning models ignore `MODEL_TEMPERATURE` |
| `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `gemini-2.5-pro` | Google AI Studio key |

The Strands agents use the matching `strands.models.*` provider and the LangGraph investigator the
matching `langchain_*` chat model; prompts, tools, A2A and telemetry are unchanged. Switching is a
`.env` edit plus `docker compose up -d` (no rebuild needed).

On AWS, pass the provider as CDK context and keep the key in Secrets Manager; the runtimes read it once
at startup:

    aws secretsmanager create-secret --name argus/model-api-key --secret-string "$OPENAI_API_KEY"
    cd infra/cdk && cdk deploy --all -c modelProvider=openai -c modelId=gpt-5 \
        -c modelApiKeySecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:argus/model-api-key-AbCdEf

On AWS, production is Bedrock-only: the agents stack refuses any other provider at synth time (ADR-0002) and only first-party Bedrock vendors (`anthropic`, `amazon`) are invokable, so Bedrock Marketplace models, which need a paid SageMaker endpoint, cannot be selected. Set `-c allowExternalModelProviders=true` only for a throwaway evaluation stack. AgentCore Memory always uses Bedrock.

## Data model

- **Ownership network.** `entities` and `edges` hold vessels, companies, people, sanction listings and flags with typed relationships. `ownership_network('vessel:<mmsi>', depth)` returns the k-hop neighbourhood with the shortest path to every sanction listing; the registry MCP tool of the same name gives it to the Investigator (with person names decrypted), and `GET /network/{mmsi}` serves the UI pseudonymously. Rendezvous edges are derived from positions with the detector's proximity rule.
- **Positions** are range-partitioned by day (`positions_pYYYYMMDD`) with no primary key, because AIS carries duplicates and a spoofed MMSI is two transmitters. `retention_policy` holds the hot window (90 days); the archiver (`services/archiver`, `make archive` locally, a daily ECS scheduled task on AWS) exports expired days to Parquet in S3, verifies, drops the partition and writes an audit event.
- **Evidence snapshots.** Every alert keeps the positions around its window and the tool outputs it cited; every completed investigation keeps 24 h of positions, the registry record, the depth-2 ownership network and each evidence item. `GET /evidence?entity_kind=&entity_id=` lists them; `GET /evidence/{id}` returns one.
- **Personal data** (`beneficial_owner`, person names) is stored only encrypted with pgcrypto. The key is `DATA_KEY` locally and a KMS-encrypted Secrets Manager secret on AWS (`DATA_KEY_SECRET_ARN`).

## Durable jobs

Sweeps and investigations are rows in `jobs`; the queue (Redis Streams locally, SQS with a dead-letter queue on AWS) carries only the id. The worker (`python worker.py`, same image as the API) claims a job, runs it with a timeout, retries transient failures with backoff, and marks the rest failed or dead with an audit event. Sweeps go straight to the Watch agent over A2A; investigations go to the orchestrator, which reports node-level progress (`investigator`, `tasking`, `report`) back through `POST /investigations/{id}/progress`. Progress is streamed on `GET /events` and shown live in the UI. Scheduled sweeps: `SWEEP_INTERVAL_MIN` locally, EventBridge Scheduler on AWS (`sweepIntervalMinutes` context, off in stop mode). The investigation policy `AUTO_INVESTIGATE_SEVERITIES` (default `high`) opens an investigation for severe alerts automatically. `GET /jobs` lists jobs.

## Investigation graph, tiers, safety, provenance

The orchestrator is code: identity and behaviour Investigator branches run in parallel over A2A, a pure merge joins them, the Tasking agent gets the extracted evidence gap, and a tool-less report node writes the Vessel of Interest report from validated JSON only. Models are chosen by tier (Watch fast, Tasking standard, Investigator and report strong; `MODEL_TIER_<ROLE>` and `MODEL_ID_<TIER>` override; `MODEL_ID` pins everything). The Investigator escalates a tier if its output fails validation; the report node retries once with the policy violations (allowed actions, evidence traceability, `agents/shared/policy.py`). MCP servers mark external free text as untrusted data and expose their version on `/health`; each investigation stores a provenance manifest (code revision, prompt hashes, per-node models and attempts, MCP versions, evidence snapshot ids).

## Evals and operations

- **Detector evals** (`tests/integration/test_detectors.py`): every injected anomaly in every scenario must be found by its AIS detector, false positives bounded. Model-free; CI runs them against a PostGIS service. Locally: `DATABASE_URL=postgresql://argus:argus@localhost:5432/argus_test pytest -m integration tests/integration` (use a scratch database; the test reloads the scenario).
- **Node evals** (`python evals/node_evals.py --api http://localhost:8000 --gate`): Watch recall/precision, Investigator schema and evidence traceability and expected content, Tasking decision match, report completion, policy cleanliness and an LLM-judge rubric (three samples at temperature 0, averaged per dimension; one sample swung a full point between identical runs). The Watch suite dismisses open alerts first, because the agent deliberately does not duplicate them. Watch precision counts false alarms only among the kinds the scenario labels (other kinds are reported as unscored), and expected keywords may list alternatives (`["PW", "Palau"]`). Install the eval deps from `evals/requirements.txt` (the inspect-ai harness has its own `evals/requirements-inspect.txt`). Cases in `evals/cases.yaml`, floors in `evals/thresholds.yaml`, results in `evals/results/`, `GET /evals`, and CloudWatch GenAI Observability with `--push`. `evals/e2e.sh` runs the suites over every scenario. The `evals` workflow runs nightly and on prompt or model changes when `EVAL_API_URL` is configured.
- **SLOs** (`GET /slo`, Prometheus `GET /metrics`): sweep and investigation p95 latency, completion ratio, alert-to-investigation lag, cost per investigation (from per-node token usage in the provenance manifest and a price table, `MODEL_PRICES_JSON` to override), latest eval recall. Alert rules in `observability/alerts.yml`, dashboard "Argus SLOs" in Grafana; on AWS the collector scrapes the same endpoint and CDK creates CloudWatch alarms and an SNS topic (`-c alertEmail=you@example.org`).

## Watch floor UI

One page, no build step (`services/ui/index.html`). Tabs for Alerts, Investigations, Tasking and Audit; the map shows the watched area, zones, vessels as circles with a course vector (length grows with speed, dimmed when stale), a ring for vessels under investigation, and tracks with start/latest markers, hover details per report and silent periods drawn as dashed red segments with their duration. Track and Esc toggle a track off; layer toggles sit top-left. Alerts filter by severity, reviewed state, text and sort order; keyboard: `j`/`k` select, `i` investigate, `t` track, `Shift+A` / `Shift+R` accept or reject. Reports open by deep link (`?investigation=<id>`), can be copied as a link, exported as JSON or printed. The header shows feed mode (replay or live AIS), time since the last report, the scenario clock and the watched area (`GET /area`).

Coverage: Argus watches the scenario's bounding box plus the `data/areas.yaml` regions named by `WATCH_AREAS` (`all` by default); live mode subscribes to every watched box on AISStream in one session, replay has data only in the scenario's box. The header's watching selector fits the map to one region and filters the vessels shown; alerts stay global. It is not a global feed.

## AWS lifecycle (all through CDK)

| Verb | Command | Effect |
|---|---|---|
| Deploy / update | `make deploy` (`make update` is the same) | Bootstraps CDK if needed and deploys or diffs the four stacks in place |
| Stop | `make stop-aws` | Redeploys with `-c paused=true`: every ECS service to 0 tasks, Aurora to 0 ACU with 15-minute auto-pause. Data, network and endpoints stay |
| Start | `make start-aws` | Redeploys with `-c paused=false` |
| Delete | `make destroy` | `cdk destroy --all`, then `cdk gc` removes the images and assets in the bootstrap bucket and ECR repository |

Nothing is created outside CloudFormation: the CloudWatch Transaction Search settings are custom resources that deploy applies and destroy reverts, and every log group (services, collector, VPC flow logs, AgentCore runtimes) is owned by a stack. The only survivor of `make destroy` is the CDKToolkit bootstrap stack itself.

Locally, `make stop` / `make start` pause and resume the compose stack with volumes intact; `make down` deletes everything including data.

Useful deploy-time context (pass via `CDK_CONTEXT="-c key=value ..."` or edit `infra/cdk/cdk.json`):

| Context | Default | Purpose |
|---|---|---|
| `uiAllowedCidr` | `0.0.0.0/0` | Restrict the public UI load balancer to an office or VPN range. Set this before real use |
| `uiCertificateArn`, `uiDomain` | empty | A domain certificate and its name; without them the deploy issues a self-signed certificate for the balancer's own name |
| `officerEmail`, `officerMfa` | from `.env` | First watch-floor account; `required` enforces TOTP |
| `natPerAz`, `retainData`, `auroraReader` | `true`, `false`, `false` | NAT per zone; deletion protection and final snapshot; a reader instance |
| `bedrockModelVendors` | `amazon` | Bedrock vendors the agents may invoke |
| `paused` | `false` | Stop mode, normally set by `make stop-aws` |

## Network layout

One VPC, three subnet tiers, VPC flow logs to CloudWatch:

- **public**: the watch-floor load balancer (HTTPS, Cognito, WAF) and the NAT gateways, ingress limited to `uiAllowedCidr`.
- **private (NAT egress)**: ECS services. NAT exists only for the external data feeds (AISStream, Copernicus, OSM, OpenSanctions).
- **agents (isolated, no NAT route)**: the AgentCore runtimes. They reach Bedrock, AgentCore, the AgentCore Gateway, the Agent Registry, CloudWatch, X-Ray, Secrets Manager, SSM and STS through interface endpoints, the tools through the gateway and the API through the internal ALB. Their security group has no default egress. An agent cannot reach the internet by any path (ADR-0007).

Aurora and Valkey accept connections only from the services security group; storage is encrypted at rest.

## Services and tools by purpose

Every row is in the repository and deployed; `docs/ROADMAP.md` lists what comes next.

### Agents and orchestration

| Purpose | Service or tool | Status |
|---|---|---|
| Watch, Tasking, Orchestrator agents | Strands Agents SDK | In place |
| Investigator agent (deliberately a second framework) | LangGraph + LangChain | In place |
| Agent hosting on AWS | Bedrock AgentCore Runtime, one runtime per agent | In place |
| Per-vessel long-term memory | Bedrock AgentCore Memory | In place, no-op locally |
| Agent-to-agent calls | A2A protocol, SigV4-signed on AWS | In place |
| Code-owned investigation graph, parallel branches | Python graph in the orchestrator, pure merge | In place |
| Scheduled sweeps | EventBridge Scheduler on AWS, loop locally | In place |
| Durable jobs with retries and dead-letter | One SQS queue and worker service per kind on AWS, Redis stream locally | In place |

### Tools the agents call

| Purpose | Service or tool | Status |
|---|---|---|
| AIS queries and anomaly detectors (gaps, spoofing, loitering, rendezvous, zone incursion) | `ais` MCP server, SQL on PostGIS | In place |
| Vessel identity, ownership, sanctions, flag history | `registry` MCP server, optional OpenSanctions API | In place |
| Zones, ports, distances, reverse geocoding | `geo` MCP server, optional OpenStreetMap Overpass and Nominatim | In place |
| Imagery archive search, next pass, tasking proposals | `imagery` MCP server, optional Copernicus Data Space | In place |
| Multi-hop ownership network for the Investigator | `ownership_network` tool on the registry server | In place |
| Governed MCP front door | Bedrock AgentCore Gateway with a Cedar policy engine, IAM inbound | In place on AWS |

### Models

| Purpose | Service or tool | Status |
|---|---|---|
| Production inference | Amazon Bedrock, Amazon Nova only, via VPC endpoint | In place |
| Dev and eval comparison across providers | Anthropic API, OpenAI, Google Gemini through `MODEL_PROVIDER` | In place, refused on AWS |
| Content guardrail on every model call | Bedrock Guardrails | In place |
| Fast, standard, strong tiers with escalation on invalid output or throttling | Config in the shared model layer | In place |

### Data

| Purpose | Service or tool | Status |
|---|---|---|
| Positions, zones, vessels, alerts, investigations, tasking, audit | PostGIS locally, Aurora Serverless v2 PostgreSQL on AWS | In place |
| Live position fan-out to the UI | Redis stream locally, ElastiCache Serverless Valkey on AWS | In place |
| Synthetic scenario with injected anomalies and ground truth | `ais-replay` service and the scenario generator | In place |
| Live AIS feed | AISStream.io websocket | In place, off by default |
| Day partitions, hot retention, archive | Native range partitions, S3 Parquet with lifecycle to Deep Archive | In place |
| Ownership network entities and edges | Tables in PostGIS with recursive CTEs | In place |
| Personal-data encryption and re-keying | pgcrypto with a KMS-encrypted data key, `make rekey-aws` | In place |

### Platform, API, UI

| Purpose | Service or tool | Status |
|---|---|---|
| REST and SSE API, human approval and review endpoints | FastAPI on ECS Fargate | In place |
| Watch-floor map UI | Static HTML with MapLibre behind nginx, public ALB | In place |
| Orchestrator invocation from the API | AgentCore InvokeAgentRuntime, HTTP locally | In place |
| Review states and append-only audit events | API and Postgres tables | In place |

### Infrastructure and lifecycle

| Purpose | Service or tool | Status |
|---|---|---|
| Local one-command stack | Docker Compose | In place |
| AWS deploy, update, stop, start, delete | AWS CDK in Python, four stacks, Makefile verbs | In place |
| Container images | uv-based Python images, amd64 for ECS, arm64 for AgentCore | In place |
| Secrets | Secrets Manager: rotated DB password, data key, feed keys; certificates issued at deploy time into ACM | In place |
| Cross-stack handoff of the orchestrator ARN | SSM Parameter Store | In place |

### Network and security

| Purpose | Service or tool | Status |
|---|---|---|
| Isolation tiers | One VPC: public, private with NAT, isolated agents | In place |
| Private access to AWS services from isolated agents | Interface endpoints for Bedrock, AgentCore, AgentCore Gateway, Agent Registry, Logs, X-Ray, Secrets Manager, SSM, STS, ECR | In place |
| Traffic audit | VPC flow logs to CloudWatch | In place |
| Watch-floor access | HTTPS only, Cognito sign-in on every public listener, AWS WAF, allowed CIDR; the API verifies the balancer's signed token | In place |
| Deploy and operator identity | `argus-deployer` (MFA administrator) and `argus-operator` (caller token for evals and scripts) roles | In place |
| Least-privilege model access | IAM scoped to first-party Bedrock model ARNs | In place |
| One IAM role per agent, tool authorization outside agent code | IAM roles; AgentCore Gateway + Policy (Cedar per role) for tools; STS-verified caller identity on agent-only API routes | In place |

### Observability

| Purpose | Service or tool | Status |
|---|---|---|
| Traces, metrics, logs from every service | OpenTelemetry SDK, Strands GenAI spans, OpenInference for LangGraph | In place |
| Local backends | OTel Collector, Tempo, Loki, Prometheus, Grafana | In place |
| LLM-centric tracing | CloudWatch GenAI Observability | In place, optional profile |
| AWS backends | AgentCore-native telemetry in CloudWatch (runtime logs, unified traces with Transaction Search, Evaluations and Policy metrics, the GenAI Observability agent views) plus the collector fan-out to the self-hosted Grafana + Tempo + Loki + Prometheus Fargate task (Grafana state on Aurora, Tempo and Loki on EFS; `GrafanaUrl`; `-c grafanaStack=false` to skip) | In place |
| Provenance manifest per investigation | Postgres, prompt hashes and versions, tool versions | In place |
| SLOs, cost per investigation, alarms | API SLIs, Grafana rules and 31 CloudWatch alarms on one SNS topic | In place |

### Evaluation

| Purpose | Service or tool | Status |
|---|---|---|
| Watch recall and precision against ground truth | `run_eval.py`, scores optionally pushed to CloudWatch GenAI Observability | In place |
| Evals runnable alongside other suites | Inspect AI task | In place |
| Per-node evals, rubric-graded reports, CI gating | `evals/node_evals.py --gate`, AgentCore Evaluations online, detector tests in CI | In place |

### Development tooling

| Purpose | Service or tool | Status |
|---|---|---|
| Lint and format | Ruff, pinned to the CI version | In place |
| Tests | pytest, dependency-free unit tests, `integration` marker | In place |
| CI | GitHub Actions: ruff, format check, pytest, compose config, cdk synth | In place |
| Claude Code helpers | `/ci-local`, `/demo-e2e`, format-on-edit hook, `CONTEXT.md`, ADRs | In place |

## Agent identity, audit and review

- **One IAM role per agent** (`argus-agent-watch`, `-investigator`, `-tasking`, `-orchestrator`). Only the orchestrator may invoke the specialist runtimes and use AgentCore Memory; every role may invoke only first-party Bedrock models.
- **Tool authorization at the gateway, caller identity on the API.** On AWS every tool call is an MCP call to the AgentCore Gateway signed with the agent's own role; the gateway's Policy engine holds one Cedar policy per role generated from `mcp-servers/tools.json` (watch: ais and geo; investigator: ais, registry and geo; tasking: imagery and geo), so an agent cannot call a tool it was not granted, and the decision is logged. The tool runtimes accept the gateway role only. Agent-only API routes keep the STS-signed caller token (`TOOL_AUTH=aws-iam`): the API forwards it to STS and allows the role or fails closed.
- **Append-only audit log.** `audit_events` records every state change (alert raised or reviewed, investigation started/completed/failed/reviewed, tasking proposed/approved/rejected, sweep requested) with the actor (verified agent role or watch officer id), time, details and trace id. A trigger rejects UPDATE and DELETE. Read it at `GET /audit`.
- **Review states.** Alerts and VOI reports start as `draft` and are shown as "AI draft" in the UI until a watch officer accepts or rejects them (`POST /alerts/{id}/review`, `POST /investigations/{id}/review`). Tasking keeps proposed/approved/rejected and now records who decided. The officer's id comes from the `X-Watch-Officer` header (the UI's "officer" box) until the load balancer gets an OIDC login; that is the known gap in this phase.
