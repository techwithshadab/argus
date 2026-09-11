# Argus API and tool reference

Base URL: `http://localhost:8000` locally (the UI proxies `/api/`), the internal ALB on AWS. JSON everywhere except `/metrics` (Prometheus text) and the SSE endpoints. Interactive docs at `/docs`.

**Identity.** Human endpoints take the officer from the load balancer's signed id token on AWS (`OFFICER_AUTH=oidc`, the email claim) and from `X-Watch-Officer` locally (`OFFICER_AUTH=header`, default `watch-officer`); non-browser callers on AWS send a bearer caller token as an allowed IAM role (`argus-operator`); the public load balancer forwards `/api/*` requests that carry a bearer token without the Cognito sign-in, and the API verifies the token. Agent-only endpoints (marked *agent*) require `Authorization: Bearer <STS-signed token>` from a role in `AGENT_ALLOWED_ROLES` when `TOOL_AUTH=aws-iam`. Officer actions (review, approve, sweep) admit only `OFFICER_ALLOWED_ROLES`: the two lists are separate so an agent role cannot approve its own tasking or review its own findings.

## Watch floor data

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (touches the database) |
| GET | `/whoami` | How the caller is signed in: `{officer, mode}`; the UI locks the officer field when `mode` is `oidc` |
| GET | `/area` | Watched area: name, bbox, mode (replay/live), scenario clock, feed stats |
| GET | `/vessels` | Latest position per vessel |
| GET | `/vessels/{mmsi}/track?hours=24` | Positions before the scenario clock |
| GET | `/zones` | GeoJSON FeatureCollection of zones |
| GET | `/stream` | SSE of live positions (`event: position`) |
| GET | `/ground-truth` | Injected anomalies of the scenario (evals only; never given to agents) |

## Alerts

| Method | Path | Purpose |
|---|---|---|
| GET | `/alerts?status=` | Alerts with `status` and `review_state` |
| POST | `/alerts` *agent* | Raise an evidenced alert; snapshots evidence; may auto-open an investigation |
| POST | `/alerts/{id}/review` | `{decision: accepted\|rejected, note?}`; records reviewer and time. 409 if already reviewed |
| POST | `/alerts/{id}/{acknowledge\|dismiss}` | Legacy verbs mapped to accepted / rejected |

## Investigations and jobs

| Method | Path | Purpose |
|---|---|---|
| POST | `/investigations/{mmsi}` | `{trigger, alert_id?}` → `{investigation_id, job_id, deduplicated}`; queues the job |
| GET | `/investigations` | List with job status and progress |
| GET | `/investigations/{id}` | Full record: report, review, manifest, progress, cost estimate |
| POST | `/investigations/{id}/progress` *agent* | `{step, status, detail}` node progress from the orchestrator |
| POST | `/investigations/{id}/complete` *agent* | `{report, trace_id, manifest}`; snapshots evidence, marks the investigation's own alerts investigated. 409 unless the investigation is running |
| POST | `/investigations/{id}/fail` *agent* | `{error, trace_id}`. 409 unless the investigation is running |
| POST | `/investigations/{id}/review` | `{decision: accepted\|rejected}` on a completed report. 409 if already reviewed |
| POST | `/sweep?hours=12` | Queue a watch sweep → `{job_id}` |
| GET | `/jobs?status=&limit=` , `/jobs/{id}` | Job records (attempts, errors, progress) |
| GET | `/events` | SSE of job progress (`event: job`) |

## Evidence, network, tasking

| Method | Path | Purpose |
|---|---|---|
| GET | `/evidence?entity_kind=&entity_id=` | Snapshots a finding cited (positions, registry, network, tool outputs) |
| GET | `/evidence/{id}` | One snapshot with its payload, personal fields redacted (the registry MCP tool is the only reader of an owner name) |
| GET | `/network/{mmsi}?depth=2` | Ownership network (persons pseudonymous) |
| GET | `/tasking` | Proposed and decided tasking requests |
| POST | `/tasking/{id}/{approve\|reject}` | Human decision on a proposed request |

## Operations

| Method | Path | Purpose |
|---|---|---|
| GET | `/audit?limit=&entity_kind=&entity_id=` | Append-only audit events |
| POST | `/evals`, GET `/evals?suite=` | Record and list eval runs |
| GET | `/slo` | Service-level indicators and targets |
| GET | `/metrics` | Prometheus exposition of the indicators |

## MCP tools (streamable HTTP at `/mcp`, `/health` lists tools and version)

**ais** (port 8001): `get_vessel_track`, `get_latest_position`, `list_vessels`, `find_ais_gaps`, `detect_mmsi_conflicts`, `detect_loitering`, `detect_rendezvous` (30 min default), `list_zone_incursions`, `find_vessels_near`, `list_open_alerts`.

**registry** (8002): `lookup_vessel`, `sanctions_screen` (synthetic list, OpenSanctions when keyed), `fleet_associations`, `flag_history`, `ownership_network(mmsi, depth)`.

**geo** (8003): `list_zones`, `point_in_zones`, `nearest_ports`, `distance_nm`, `reverse_geocode` (OSM when `GEO_USE_OSM=true`).

**imagery** (8004): `search_sentinel_scenes`, `estimate_next_pass`, `create_tasking_request` (creates `proposed` only), `list_tasking_requests`.

Callers by server on AWS: ais ← watch, investigator; registry ← investigator; geo ← watch, investigator, tasking; imagery ← tasking; API ← watch, orchestrator.

## A2A

Each specialist serves the A2A protocol (JSON-RPC `message/send`) on port 9000 in its container (compose publishes 9001 watch, 9002 investigator, 9003 tasking), with an agent card at `/.well-known/agent-card.json` (the a2a-sdk also answers the superseded `/.well-known/agent.json`), `/ping` for health and `/provenance` for its model, tier and prompt hash. The orchestrator serves the AgentCore HTTP contract (`POST /invocations`, `/ping`) on 8080 with payloads `{"mmsi", "trigger", "investigation_id", "alert"}` or `{"action": "sweep", "hours"}`.

## Contracts (`agents/shared/schemas.py`, schema version 1.1)

`AnomalyAlert`, `InvestigationFindings` (with `scope` and `provenance`), `TaskingRecommendation`, `VesselOfInterestReport`, `Evidence {source, summary, reference}`.
