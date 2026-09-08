# Argus runbook

## Local

| Task | Command |
|---|---|
| First run | `cp .env.example .env` (Apple Silicon: `POSTGIS_IMAGE=imresamu/postgis:17-3.5`), set `AWS_PROFILE` with Bedrock Nova access, `make up` |
| Watch floor | http://localhost:8088 · API docs http://localhost:8000/docs · Grafana http://localhost:3000 (admin / `GRAFANA_ADMIN_PASSWORD`, default admin) · Prometheus http://localhost:9090 |
| Demo | `make sweep`, `make investigate`, or the UI buttons; `./scripts/demo.sh` |
| Pause / resume / delete | `make stop` · `make start` · `make down` (deletes volumes) |
| Logs | `make logs` (agents), `docker compose logs -f api worker` |
| CI gates | `/ci-local` or: `uvx ruff@0.16.5 check .`, `uvx ruff@0.16.5 format --check .`, `pytest -q -m "not integration"`, `docker compose config -q` |
| Detector evals | create a scratch DB, then `DATABASE_URL=postgresql://argus:argus@localhost:5432/argus_test DATA_KEY=x pytest -m integration tests/integration` |
| Node evals | `python evals/node_evals.py --api http://localhost:8000 --gate`; all scenarios: `./evals/e2e.sh` |
| Archive | `HOT_DAYS=0 DRY_RUN=true make archive` to preview; `make archive` to run |
| Schema change | add `data/sql/NNN_*.sql` (idempotent); restart `ais-replay` to apply |

## AWS

![Argus technical architecture on AWS with official service icons: the watch floor and feeds outside, the edge, jobs, deployment and identity along the top, the VPC by tier with the isolated agents and MCP servers, AgentCore, Bedrock and the AWS monitoring column](diagrams/technical-architecture.png)

Prerequisites: credentials with admin on the target account (a deploy role, not root, for anything past a demo), Docker with buildx (arm64 for AgentCore, amd64 for ECS), Node 20+, Python 3.12, Bedrock model access for Amazon Nova enabled in the region.

| Task | Command | Notes |
|---|---|---|
| Deploy or update | `make deploy` | Bootstraps CDK, deploys 4 stacks, prints the UI URL. 30–40 min first time |
| First officer account | `CDK_CONTEXT="-c officerEmail=officer@example.org" make deploy` | Cognito emails a temporary password; more officers with `aws cognito-idp admin-create-user --user-pool-id <OfficerPoolId> --username <email> --user-attributes Name=email,Value=<email> Name=email_verified,Value=true` |
| Require MFA | `-c officerMfa=required` | TOTP enrolment at first sign-in; optional by default |
| Restrict the UI | `CDK_CONTEXT="-c uiAllowedCidr=203.0.113.0/24 -c uiCertificateArn=arn:aws:acm:… -c uiDomain=watch.example.org" make deploy` | Optional: the watch floor already needs a sign-in. A certificate for your own domain (plus the domain, which becomes the sign-in callback) replaces the self-signed one (see [Signing in](#signing-in)) |
| Evals against AWS | `make eval-aws` | Assumes `argus-operator` (output `OperatorRoleArn`), runs `evals/node_evals.py --gate --push` against `UiUrl/api` with a caller token |
| Alerts by email | `CDK_CONTEXT="-c alertEmail=you@example.org" make deploy` | Confirm the SNS subscription email |
| Stop (keep data) | `make stop-aws` | ECS to 0 tasks, Aurora auto-pauses, scheduler off. About $140/month for network fixtures |
| Start | `make start-aws` | ~10 min |
| Delete everything | `make destroy` | Destroys 4 stacks, garbage-collects bootstrap assets. Under $2/month idle afterwards |
| Change sweep cadence | `-c sweepIntervalMinutes=30` | 0 disables the schedule |
| Keep the data | `CDK_CONTEXT="-c retainData=true" make deploy` | Aurora deletion protection and a final snapshot instead of a delete; switch it off before `make destroy`. Backups keep 7 days either way |
| Aurora reader | `-c auroraReader=true` | A second instance that scales with the writer (about $43/month idle) |
| One NAT gateway | `-c natPerAz=false` | Default is one per zone (about $33/month more, paused included) |
| Transaction Search | `CDK_CONTEXT="-c transactionSearch=false" make deploy` to skip | On by default: CloudWatch GenAI Observability and AgentCore Evaluations read the runtimes' telemetry through it (ADR-0012). Switching the account trace destination needs the Application Signals and CloudTrail channel permissions the stack grants its custom resource |
| Live AIS and real sanctions | set `AIS_MODE=live`, `AISSTREAM_API_KEY` and optionally `OPENSANCTIONS_API_KEY` in `.env` (or the environment), then `make deploy`; `WATCH_AREAS` (default `all`) names the `data/areas.yaml` regions watched besides the scenario's box | The stack creates the secrets, the script stores the keys and restarts the tasks; every clock switches to wall time |
| Model tiers | `MODEL_ID_STRONG=us.amazon.nova-premier-v1:0` on the runtimes (agents stack env) | Or `MODEL_ID` to pin all |

## Deploying without root

The network stack creates `argus-deployer`, an administrator role assumable by any principal in the account that signed in with MFA (or by `-c deployerPrincipalArn=<arn>` alone). Create one IAM user (or an Identity Center permission set) with MFA and only `sts:AssumeRole` on that role, then put the role in a profile and stop using root:

```ini
[profile argus]
role_arn = arn:aws:iam::<account>:role/argus-deployer
source_profile = <the user's profile>
mfa_serial = arn:aws:iam::<account>:mfa/<user>
region = us-east-1
```

`AWS_PROFILE=argus make deploy` then prompts for the MFA code; CDK and every script in `scripts/` use the same profile. The account's first deploy needs credentials that can bootstrap CDK and create the role; every later one uses the role.

## Re-keying personal data

`beneficial_owner` and person names are encrypted with the data key in Secrets Manager. `make rekey-aws` runs `rekey.py` as a one-off task on the API image: it rewrites every encrypted row under a new key in one transaction (`rekey_personal_data`, `data/sql/009`) and then stores the key in the secret; readers that cached the old key re-read it on their next decrypt (`datakey.decrypting`). Locally `make rekey` prints the new key for `.env`. `python rekey.py --dry-run` counts the rows first.

## Signing in

The public load balancer is HTTPS only and every request to the UI or Grafana goes through a Cognito sign-in (user pool `argus-officers`, no self sign-up; ADR-0018). The officer's email from the id token is what the API records on every review, approval and sweep; the officer field in the UI is read-only on AWS. Sessions last one watch shift (8 h).

Without `-c uiCertificateArn` the listener carries a self-signed certificate that the deploy generates for the balancer's own name (`SelfSignedCert` custom resource; the private key exists only inside ACM). Browsers warn once per profile; the session is encrypted either way. A certificate for a domain you control (ACM, DNS-validated) removes the warning and the Cognito callback URLs then follow the balancer's DNS name as before.

Tooling that is not a browser (evals, demo scripts) sends a bearer caller token on `/api/*`; the balancer forwards those without the sign-in and the API verifies the token against IAM (`TOOL_ALLOWED_ROLES`: the agent roles and `argus-operator`). `make eval-aws` does the role assumption; by hand: `aws sts assume-role --role-arn <OperatorRoleArn> --role-session-name evals`, export the three credentials, then `EVAL_AUTH=aws-iam EVAL_TLS_VERIFY=false python evals/node_evals.py --api https://<alb>/api --gate --push`. A root or IAM-user principal is not a role and is refused by design; `-c operatorPrincipalArn=` narrows who may assume the operator role (default: the account).

AWS WAF (`argus-watch-floor`) sits on the balancer: a per-IP rate limit (2000 requests / 5 min) and the AWS managed rule groups for IP reputation, common web exploits and known bad inputs. Blocked requests are visible with `aws wafv2 get-sampled-requests`; the `argus-waf-blocked` alarm fires on a surge.

## Health checks after a deploy

1. `aws cloudformation describe-stacks --stack-name argus-platform --query "Stacks[0].Outputs"` → `UiUrl`.
2. `UiUrl` redirects to the Cognito sign-in (accept the self-signed certificate if no domain certificate was given); after sign-in the UI loads, the officer field shows your email, the header shows the AIS mode and a live clock within a minute.
3. `curl $API/health` from inside the VPC, or check ECS service health in the console: all services 1/1.
4. AgentCore runtimes `READY` (console → Bedrock AgentCore → Runtimes); their log groups `/aws/bedrock-agentcore/runtimes/argus_*` show `MCP servers reachable` and no `RuntimeError`.
5. Trigger a sweep from the UI; watch `GET /jobs` go `queued → running → succeeded` and alerts appear.
6. Open a high alert's investigation (auto-opened by policy); progress steps appear; report lands as AI draft; `GET /investigations/{id}` has a manifest with node models and MCP versions.

## Observability on AWS

On AWS, agent reasoning, tool calls and unified traces are in the `/aws/bedrock-agentcore/runtimes/argus_*` log groups and in CloudWatch's GenAI Observability agent views (sessions, traces, evaluation scores); platform services log to `/argus/services`; SLIs are at `/api/slo` and in Prometheus. The same Grafana, Tempo, Loki and Prometheus images as the local stack run on AWS as one Fargate task (Grafana's state in a `grafana` database on Aurora, Tempo and Loki on EFS, Prometheus on the task's disk; `GrafanaUrl`, port 3000 on the public load balancer under the same CIDR rule; user `admin`, password in the secret `GrafanaAdminSecretArn`). Grafana reads CloudWatch for the AgentCore, Bedrock and AWS-resource panels and links into the GenAI Observability views. Turn the task off with `-c grafanaStack=false`.

## The board

Grafana (local http://localhost:3000, AWS `GrafanaUrl`) carries one board, `argus`, generated from one source (`observability/grafana/build_dashboards.py`, `make dashboards`), so thresholds, links and datasources stay consistent. It is laid out for how often something is needed:

- **KPI strip** (always visible): feed freshness, vessels reporting, alerts needing a decision, oldest unreviewed, cases in progress, tasking awaiting approval, cost today, latest eval recall. Each stat is coloured against the SLO target the API exports.
- **Watch floor: what needs a decision now** (open): open alerts by severity, the backlog over time, and what the Watch agent raised, latest first.
- **Pipeline health and service levels** (collapsed): sweep and investigation p95 latency against target, alert-to-investigation lag, completion and error-budget burn, jobs and dead letters, errors in agent logs, firing alerts.
- **Agents and tools** (collapsed): MCP tool calls and p95 per tool, model calls per model, span rate and latency per service, the service graph from Tempo.
- **One investigation** (collapsed): set `$investigation` and `$mmsi`; the traces, the node timeline and everything the tools and agents logged for that hull.
- **Models, cost and quality** (collapsed): tokens by node and model, cost per investigation, rejections and escalations, eval scores by suite, the change-control note.
- **AgentCore, Bedrock and AWS resources** (AWS only, collapsed): online evaluation scores from AgentCore Evaluations, AgentCore invocations and errors by operation (runtime, gateway, policy, memory), guardrail interventions, Bedrock throttles, ECS, Aurora.

Every threshold is the SLO target the API exports or a written-down operator judgement. Alert rules are provisioned with the board (`observability/aws/alerting.yaml`) and route to the SNS topic: feed stale, completion below target, spend above budget, alerts waiting too long, rejections rising, eval gate failed, API metrics not scraped. They all read the API's `/metrics` through the collector, so the last one is the rule that says the others are blind.

## Alarms

CloudWatch alarms (`infra/cdk/stacks/alarms.py`, output `AlarmCount`) watch the layer under the Grafana rules on AWS-side metrics, so they keep paging when the API, the collector or Grafana is down. Alarm and recovery both go to the SNS topic `argus-alerts` (output `AlertsTopicArn`; `-c alertEmail=` adds an email subscription). Availability alarms (`*-down`) are not created in stop mode.

| Alarm | Meaning | First checks |
|---|---|---|
| `argus-jobs-oldest-message`, `argus-sweeps-oldest-message` | A queued job is older than its whole timeout ladder (40 min investigations, 25 min sweeps) | `argus-worker-down` / `argus-sweep-worker-down`; worker logs for `reclaimed` or Bedrock 429; `GET /jobs?status=running` |
| `argus-jobs-backlog`, `argus-sweeps-backlog` | More than 50 investigations or 3 sweeps waiting for 15 min | Worker concurrency (`WORKER_CONCURRENCY`); a sweep raising far more alerts than usual (`SWEEP_MAX_CANDIDATES`) |
| `argus-jobs-dlq`, `argus-sweeps-dlq`, `argus-sweep-schedule-dlq` | A message was dead-lettered after four deliveries, or the scheduler could not enqueue a sweep | `GET /jobs?status=dead`, `jobs.error`; the scheduler DLQ means SQS or KMS denied the scheduler role |
| `argus-public-alb-5xx`, `argus-internal-alb-5xx` | The load balancer itself answered 5xx (no healthy target, or a target timed out) | The matching `*-down` alarm; target health in the ECS console events of the service |
| `argus-public-alb-target-5xx`, `argus-internal-alb-target-5xx` | The UI, API or Grafana returned 5xx | API logs (`/argus/platform`, stream `api`); Aurora reachable? |
| `argus-api-down`, `argus-ui-down`, `argus-grafana-down` | No healthy target for three minutes | ECS service events: crash loop rolled back? image pull? Aurora secret? |
| `argus-worker-down`, `argus-sweep-worker-down`, `argus-collector-down`, `argus-ais-replay-down` | No running task for five minutes (Container Insights) | Same as above; the collector task must exist before any other task starts (Service Connect names) |
| `argus-aurora-cpu`, `argus-aurora-capacity`, `argus-aurora-local-storage` | CPU > 80% or ACUs > 90% of the maximum for 15 min; local storage under 2 GiB | Slow queries on `positions` (partitions present?); raise `serverless_v2_max_capacity` in `data_stack.py` |
| `argus-degraded-branches` | More than two Investigator branches degraded in an hour (`Argus/Investigations`, published by the API on completion) | The manifest's `nodes` with `degraded`; Bedrock throttling (`escalated_to` in the same manifest means the tier escalation ran); a tool server down |
| `argus-waf-blocked` | The web ACL blocked more than 100 requests in five minutes | `aws wafv2 get-sampled-requests --web-acl-arn ... --rule-metric-name ALL --scope REGIONAL --time-window ...`: an attack, or a managed rule matching legitimate officer traffic (count that rule before disabling it) |
| `argus-nat-<n>-port-allocation`, `argus-nat-<n>-packets-dropped` | The NAT gateway could not allocate a source port or dropped packets | A feed client leaking connections (`aisstream`, OpenSanctions); with `-c natPerAz=true` each zone has its own gateway |
| `argus-eval-<suite>` | The latest `evals/node_evals.py --push` run of a suite missed a floor in `evals/thresholds.yaml` | Compare the failing run's `code_revision` and prompt hashes with the last passing one; do not promote the change |

## Pilots behind flags (ADR-0013)

- **Tasking through the AgentCore Harness.** `make deploy CDK_CONTEXT="-c taskingViaHarness=true"` (or `cdk deploy -c taskingViaHarness=true`) sets `TASKING_HARNESS_ARN` on the orchestrator, which then calls `InvokeHarness` for the tasking node. The harness `argus_tasking` exists either way (output `TaskingHarnessArn`) and can be tried by hand with `aws bedrock-agentcore invoke-harness`. Promote it only after `evals/node_evals.py --gate` passes with the flag on.
- **Optimization.** The orchestrator's configuration bundle (`argus_orchestrator`, output `OrchestratorBundleArn`) holds the report prompt and model. `agentcore run ab-test` and recommendation runs attach a bundle version to requests through baggage; the report node then uses that version's `report_system_prompt`. Without a bundle on the request the managed prompt applies.
- **Discovery.** Agents resolve each other from the registry (`/argus/registry-id`). A record that is not `APPROVED`, or a search failure, falls back to `A2A_*_URL` and logs `registry lookup ... failed`; `aws agent-registry-control list-registry-records --registry-id <id>` shows the records.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| AgentCore runtime `CREATE_FAILED`: `subnets are in unsupported availability zones` | The VPC picked a zone AgentCore does not support | `agentcoreZoneIds` in `cdk.json` lists the supported zone ids; deploy.sh resolves them and filters the agent subnets. Update the list if AWS changes it |
| Runtime log shows `Failed to pull image ... i/o timeout` on an S3 URL | Agents security group cannot reach the S3 gateway endpoint | The network stack allows 443 to the S3 prefix list; redeploy it, then invoke again |
| Agent container exits with `MCP servers not reachable` | MCP server unhealthy, or agent started before it | Check the MCP `/health`; compose `depends_on` and AgentCore start order |
| `KeyError` in a prompt | Literal `{` `}` in a prompt file | Double the braces |
| Agent fails at start with `421 Misdirected Request` from `/mcp` | FastMCP DNS-rebinding host check | `serve.py` sets `transport_security` off; make sure the MCP image is current |
| 403 from the tool gateway on AWS | The agent's role has no Cedar policy for that tool (`ToolPolicy*` in the agents stack, generated from `mcp-servers/tools.json`); regenerate the inventory after adding a tool. 401/403 from the API: caller's role not allowed or `TOOL_AUTH` mismatch |
| Report rejected by policy twice | Model recommended a forbidden action or cited a source no tool produced | See `manifest.nodes[report].policy_problems`; tune the prompt; evals will catch regressions |
| Job `dead` | Transient failures exhausted retries | `GET /jobs/{id}` for the error; fix the cause; re-request (new job) |
| `/area` says `positions extent` / `unknown` | Replay task has not published `scenario_meta` yet (or an old schema) | Wait for the replay task to finish `apply_schema`; check its log |
| Services log `NameResolutionError` for `otel-collector` | Task started before the collector's Service Connect entry existed | `aws ecs update-service --force-new-deployment` on that service; the CDK dependency prevents it on fresh deploys |
| Collector logs `OTLP API is supported with CloudWatch Logs as a Trace Segment Destination` | Account trace destination is X-Ray classic | Expected with the default `awsxray` exporter; enable `-c transactionSearch=true` only if you want OTLP-to-CloudWatch |
| Positions missing after a schema change locally | `data/sql/*.sql` runs only on a fresh `pgdata` volume | `make down` to rebuild volumes; the shared volume is mounted at `/app/shared`, never over `/app/data` |
| Map blank | Basemap CDN slow | Data layers draw first on a plain chart; the basemap merges when it arrives |

## Backups and retention

Aurora automated backups (7 days) cover findings and audit; positions older than 90 days live in the S3 archive as Parquet (`positions/day=YYYY-MM-DD/`), moved to Glacier Instant Retrieval after 30 days, to Deep Archive after a year and deleted after seven. `retention_policy` documents the classes. The audit log is append-only and never pruned.

The database password rotates every 30 days (Secrets Manager hosted rotation). Running tasks keep working: the API, the worker, the replay task and the tool servers re-read the secret the first time a connection fails and rebuild their pools (`services/api/dbconn.py` and its copies); Grafana reads the secret at start, so its task fails its health check after a rotation and ECS replaces it within a few minutes (`argus-grafana-down` may fire once). The internal load balancer's API listener is HTTPS with a deploy-time certificate; agents trust it through the SSM parameter `/argus/internal-ca`.


### A report node shows `guardrail_blocked` or an investigator branch says `guardrail blocked the request`

The Bedrock Guardrail intervened on a machine-built prompt or on a report describing suspected misconduct. Filters are calibrated low on purpose (see ARCHITECTURE.md); check `InvocationsIntervened` by policy type before changing them. The report node retries once with a plainer correction list; a branch that is blocked twice is marked degraded and the other branch carries the investigation. `AWS/Bedrock/Guardrails` `InvocationsIntervened` (by policy type) shows what fired; the guardrail's own trace is in the model call span. If a legitimate prompt is blocked repeatedly, adjust the guardrail's topic definitions in `agents_stack.py`, not the filter strengths.

### CloudWatch GenAI Observability shows several sessions for one investigation

Every span must carry `session.id` equal to the investigation id. The collector's `transform/session` processor folds the AgentCore runtime session ids (`rt-<investigation id>-<role>`) back to the investigation id; check that the collector config contains it and that the worker and the orchestrator's A2A calls pass those ids.


### A sweep summary shows `deferred` candidates

The Watch agent reviews at most `SWEEP_MAX_CANDIDATES` (default 25) detector candidates per sweep, ranked by kind, zone context and duration; the rest are deferred and come back on the next sweep if still detected and not already open. In live mode a two-hour window over a busy area can hold a hundred short AIS gaps, most of them receiver coverage. Raise the cap or shorten the sweep interval if `deferred` stays high; a candidate the model failed to review is raised as a low alert that says so.


### Scheduled sweeps arrive late

Sweeps and investigations share one queue and one worker pool, and a burst of auto-opened investigations (each two to three minutes) delays the sweeps queued behind them. Raise `WORKER_CONCURRENCY`, or open investigations for `high` alerts only. A separate sweep queue with its own consumer is the structural fix and is on the roadmap.
