# Argus architecture audit

Date: 2026-09-08. Scope: the whole repository and the AWS deployment (us-east-1, live AIS mode). Method: every claim below points at code, a template, or a measured run; nothing is asserted from intent. Ratings: **Strong** (production-shaped and verified), **Adequate** (works, with a named gap), **Weak** (missing or unproven).

## Summary

| Dimension | Rating | One-line verdict |
|---|---|---|
| Tech stack | Strong | Strands + LangGraph on AgentCore, MCP tools over PostGIS, FastAPI, CDK with cdk-nag; every dependency pinned |
| Design and orchestration | Strong | Code-owned graph, parallel branches, deterministic merge, single LLM in the orchestrator, tier escalation on invalid output and on throttling |
| Data structures and algorithms | Strong | Day-partitioned positions, GiST and (mmsi, ts) indexes, detectors as SQL window and spatial queries, recursive-CTE ownership graph |
| Database design | Strong | Idempotent migrations, IAM authentication, seven-day backups, monthly password rotation with re-reading consumers, operator re-keying of personal data; a reader and deletion protection behind flags |
| Security | Strong | Cognito sign-in and WAF on an HTTPS-only public balancer, HTTPS on the internal API listener, per-agent IAM, STS-signed callers, isolated subnets, Bedrock-only, guardrail, deploy and operator roles instead of root |
| Compliance and governance | Strong | Encrypted personal data, append-only audit, retention classes with lifecycle to Deep Archive, review states vs approvals, eighteen ADRs, access logs on balancers and archive |
| Safety | Strong | Human gate in the API, policy check on every report, untrusted marking of every feed text, Bedrock Guardrail without PII masking |
| Reliability | Adequate | Queue and worker per kind, heartbeat and reclaim, timeout ladder, circuit breakers, degraded branches, 36 alarms; agents in one zone, in-flight graph state lost on a worker restart |
| Scalability | Adequate | Serverless Aurora and Valkey, four investigation workers; no autoscaling |
| Observability | Strong | OTel end to end, AgentCore telemetry in CloudWatch with Transaction Search, self-hosted Grafana stack, Grafana rules plus CloudWatch alarms that stay up when the API is down |
| Identity per agent | Strong | One IAM role per runtime and task, allowlists per tool server, orchestrator alone may invoke specialists |
| Prompt and tool versioning | Strong | Managed prompt versions, prompt hashes of the text that ran, tool server version and code revision in every manifest |
| Model choice by task | Strong | Tiers per role (Lite, 2 Lite, Pro, Premier as the step-up), escalation on invalid output and on availability errors |
| Graph RAG and graph engineering | Adequate | Ownership graph with sanction-path queries; no embeddings by design, small graph, no live registry feed |
| AI evals | Adequate | Detector evals in CI, node evals with floors and a pushed gate metric that alarms, LLM judge; judge uncalibrated |

## 1. Tech stack

**Evidence.** Agents: Strands (`agents/watch`, `agents/tasking`, the report node), LangGraph (`agents/investigator`), A2A between them through the agents gateway, AgentCore runtimes, memory, gateways, registry, identity, evaluations (`infra/cdk/stacks/agents_stack.py`, ADR-0011, ADR-0013). Tools: four MCP servers over streamable HTTP (`mcp-servers/`), each versioned. Platform: FastAPI, psycopg, Redis Streams locally and SQS on AWS, ECS Fargate, CDK in Python with cdk-nag on every synth (`infra/cdk/app.py`, `stacks/nag.py`).

**Assessment.** Fit for purpose. Two frameworks are a deliberate, time-boxed choice (ADR-0017): both speak MCP, so tools are shared, and the model, telemetry and identity glue lives in `agents/shared/` once.

**Gaps.** Node 20 for CDK is past end of life; upgrade to 22.

## 2. Design, routing and orchestration

**Evidence.** The orchestrator is a code graph (`agents/orchestrator/app.py`): two Investigator branches in parallel, a pure merge (`shared/graph.py::merge_findings`, unit-tested), Tasking with the extracted evidence gap, a tool-less report node with a policy check and one retry, then persistence to Aurora before Memory (ADR-0015). Sweeps are a deterministic detector pre-pass plus one judgement call (`shared/sweep.py`). Escalation to the next tier fires on invalid output and on provider availability errors (`shared/models.py::model_unavailable`).

**Assessment.** Parallel where the work is independent, sequential where a step needs the previous one; nothing a model says can change the sequence.

**Gaps.** The Tasking node has no fallback when the imagery catalogue is unreachable beyond the placeholder scene list.

## 3. Data structures and algorithms

**Evidence.** `positions` is range-partitioned by day with no primary key by design, with `(mmsi, ts)` and GiST indexes (`data/sql/004_*`). Detectors (`mcp-servers/servers/ais.py`) are SQL: gaps by `lag()`, MMSI conflicts by simultaneous distant reports, loitering by extent, rendezvous by `ST_DWithin`, incursions by `ST_Intersects`. The ownership graph is `entities` and `edges` walked by a recursive CTE.

**Gaps.** Rendezvous is O(n²) per window over vessels in the box; fine for hundreds. No materialised view for the latest position per vessel.

## 4. Database design

**Evidence.** Nine idempotent migrations applied on every start (`data/sql/`), audit trigger rejects update and delete, review states on findings, approval on actions, evidence snapshots, manifests as JSONB, `scenario_meta` for cross-host handoff, retention classes, `rekey_personal_data` for the data key. Aurora Serverless v2 with IAM authentication, seven-day backups, monthly hosted rotation of the password (`data_stack.py`); every consumer re-reads the secret on failure (`services/api/dbconn.py` and its copies). `-c retainData=true` adds deletion protection and a final snapshot; `-c auroraReader=true` adds a reader.

**Gaps.** No point-in-time restore test has been run. No connection pooler in front of Aurora.

## 5. Security

**Evidence.** Cognito user pool with the balancer's authenticate action on every public listener, HTTPS only, AWS WAF (rate limit and managed rule groups), the API verifying the balancer's signed id token and recording the email (ADR-0018, `services/api/officerauth.py`); HTTPS on the internal API listener with a deploy-time certificate trusted through SSM; one IAM role per agent runtime (ADR-0001); tool servers and agent-only routes verify STS-signed caller tokens and fail closed (`callerauth.py`); agents in isolated subnets with no NAT and interface endpoints only (ADR-0007); Bedrock through a VPC endpoint with the `amazon` vendor alone grantable (`lifecycle.py`); `argus-deployer` (MFA administrator) and `argus-operator` roles; cdk-nag with justified suppressions (`stacks/nag.py`); access logs on both balancers and the archive.

**Gaps, in priority order.**
1. Without a domain certificate the public certificate is self-signed; supply `uiCertificateArn` and `uiDomain` for real use, then CloudFront in front of the balancer.
2. `argus-operator` is assumable by any principal in the account by default; set `operatorPrincipalArn`.
3. Grafana keeps its own login behind the Cognito sign-in; it could trust the balancer's identity header instead.

## 6. Compliance and governance

**Evidence.** Append-only audit of every state change with an authenticated actor; findings carry `review_state`, actions carry approval, never conflated (`CONTEXT.md`); retention policy with 90-day hot positions, Glacier at 30 days, Deep Archive at a year, expiry at seven; data classification in `docs/SECURITY.md`; eighteen ADRs; documentation counts checked against the repository in CI.

**Gaps.** No formal data-subject process for the encrypted personal data; re-keying is an operator procedure (`make rekey-aws`), not a schedule.

## 7. Safety

**Evidence.** Agents can only create `proposed` tasking rows; approve and reject are human endpoints; every report passes `shared/policy.py` or fails closed after one retry; every free text from a feed passes `common/safety.untrusted()` (pinned by `tests/test_safety.py`); guardrail on every model call without PII masking so reports can name owners.

**Gaps.** The policy's allowed-action patterns are regular expressions; an eval case per forbidden action would keep them honest.

## 8. Reliability

**Evidence.** One queue and one worker service per job kind, a timeout ladder pinned by tests, a visibility heartbeat and `reclaim` on redelivery (ADR-0016); idempotency keys; deployment circuit breakers with rollback; 36 CloudWatch alarms on AWS-side metrics with the runbook table pinned by a test; the `argus-api-scrape-lost` rule that says when the Grafana rules are blind; degraded branches counted as a metric and alarmed.

**Gaps.** Agents in a single availability zone (AgentCore zone restriction with the two-zone VPC; a new VPC is needed to add one); a worker restart loses the in-flight graph state and restarts the job (a checkpointer that the isolated agents can reach is the roadmap item); no synthetic probe from outside the VPC.

## 9. Scalability

**Evidence.** Serverless Aurora and Valkey scale on their own; four investigation workers and one sweep worker; sweeps are bounded by the interval and `SWEEP_MAX_CANDIDATES`; positions partition by day and expire.

**Gaps.** No ECS autoscaling on the workers or the API; AgentCore concurrency per runtime is the service default.

## 10. Observability

**Evidence.** OpenTelemetry in every process; AgentCore's unified telemetry in CloudWatch with Transaction Search (ADR-0012); the collector fans platform telemetry to the self-hosted Tempo, Loki and Prometheus and holds no AWS permissions; one generated Grafana board; Grafana rules for the application SLOs and CloudWatch alarms for the layer beneath them, both to one SNS topic; the eval gate published as a metric.

**Gaps.** The Grafana task self-heals through its health check after a password rotation, so `argus-grafana-down` fires once a month.

## 11. Prompt, tool and code versioning

**Evidence.** Every manifest records the code revision, the hash and managed version of the prompt that ran (a bundle version when the override pilot is on, ADR-0014), each node's provider, model, tier, attempts, usage and escalations, and every MCP server's version.

**Gaps.** The repository needs its first commit for `code_revision` to read anything but `unknown`.

## 12. Model choice by task complexity

**Evidence.** Tiers per role: Nova Lite for sweeps, Nova 2 Lite for tasking, Nova Pro for investigation branches and the report, Nova Premier as the step-up; escalation on invalid output and on throttling or unavailability. Cost per investigation measured at 2 to 8 cents.

**Gaps.** Size-based routing (long tracks or many associations to Pro, short to Lite) would cut cost further.

## 13. Graph RAG and graph engineering

**Evidence.** Ownership and association graph in Postgres returning the k-hop neighbourhood and the shortest path to a sanction listing; rendezvous edges derived from positions; the Investigator's identity branch queries and cites it.

**Assessment.** The questions are "who controls this vessel, is anyone on the path listed, who has it met": joins, not similarity search. Embeddings would add cost without answering them.

**Gaps.** Live vessels get identity from AIS static data but no ownership edges until a registry feed is connected.

## 14. AI evals

**Evidence.** Detector evals in CI against PostGIS; node evals with floors per suite (`evals/thresholds.yaml`) that push `gate_passed` to CloudWatch and alarm per suite; AgentCore Evaluations online per agent; the eval gate as a Grafana rule; `make eval-aws` runs the suite against the deployment as the operator role.

**Gaps.** The judge is one prompt with no calibration set; live mode has no ground truth, so evals mean nothing there by design.

## Recommended order of work

1. A domain certificate and CloudFront in front of the balancer.
2. Commit the repository so manifests carry a real revision; narrow `operatorPrincipalArn`.
3. A checkpointer the isolated agents can reach, and the asynchronous hand-off (ADR-0016).
4. Autoscaling on the workers and API; a new VPC with a second supported zone for the agents.
5. A registry feed for live vessels; a calibration set for the judge.
