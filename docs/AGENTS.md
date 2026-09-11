# The four agents

Argus runs four agents on Amazon Bedrock AgentCore. Three of them judge; the fourth is a
state machine written in code that decides what runs and when. This page says plainly what
each one is for, which tools it calls at which moment, and what it is allowed to change.

The rule the whole design turns on: **agents propose, officers decide.** No agent can approve
a collection, accept an alert, or mark an investigation reviewed. Those are officer endpoints,
and the separation is enforced in the API rather than left to convention
(`_AGENT_ROUTES` in `services/api/main.py`).

## At a glance

| Agent | What it is for | Framework | Model tier | Tool servers | Runs when |
|---|---|---|---|---|---|
| **Watch** | Decides which detector candidates deserve an officer's attention | Strands | `fast` — Nova Lite | `ais`, `geo` | Every 30 minutes (EventBridge Scheduler → SQS) |
| **Investigator** | Does the hour of research behind one alert | LangGraph ReAct | `strong` — Nova Pro | `ais`, `registry`, `geo` | Twice per investigation, in parallel branches |
| **Tasking** | Decides whether a satellite re-look would settle the question, and proposes one | Strands | `standard` — Nova 2 Lite | `imagery`, `geo` | Once per investigation, after the branches merge |
| **Orchestrator** | Runs the investigation graph and writes the report | LangGraph, code-owned | `strong` for the report node only | none | Once per investigation, whether an officer opened it or the API's auto-rule did |

Investigator is deliberately a different framework from the Strands agents. A2A and MCP make
the framework a per-agent decision rather than a platform-wide one, and the deployment proves
it by running both.

---

## Watch — triage

**The problem it solves.** Five deterministic SQL detectors turn twelve hours of positions
into candidates. On live traffic that is far more than an officer can read. Watch decides
which ones are worth raising.

**What it does *not* do.** It never supplies the MMSI, the kind, or the time window.
`list_candidates` builds each candidate in code with fixed identifiers and evidence, and the
agent only chooses. When the model was allowed to name the vessel, Nova Lite copied one
vessel's gap window onto another and eval recall swung between 0.4 and 0.8 run to run.

| Step | Tool | Why, at that moment |
|---|---|---|
| 1 | `ais.find_ais_gaps` | The gap detector: silence longer than the threshold |
| 1 | `ais.detect_loitering` | Slow or circling movement over a long window |
| 1 | `ais.detect_rendezvous` | Two hulls close and slow at the same time |
| 1 | `ais.detect_mmsi_conflicts` | One identity reporting from two places |
| 1 | `ais.list_zone_incursions` | A hull inside a declared zone it should not be in |
| 2 | `geo.point_in_zones` | Was the vessel inside a declared anchorage or exercise area? |
| 2 | `geo.nearest_ports` | Open water, or a few miles off a berth? |
| 3 | `ais.list_open_alerts` | Is this already raised, or a repeat for the same hull? |

| Action it can take | Effect | Guard |
|---|---|---|
| `raise_alert(candidate_id, …)` | Creates an alert, `review_state=draft`, marked **AI draft** | Requires `started_at`/`ended_at`; the kind is normalised against `ALERT_KINDS` |
| `dismiss_candidate(candidate_id, reason)` | Drops the candidate with a recorded reason | **Refused** for definitional anomalies — see below |

**Non-dismissible by design.** An MMSI reporting from two places, and a rendezvous outside any
declared anchorage, can only be raised. Nova Lite dismissed the scenario's at-sea transfer as
"normal operations" about once in two runs, so that judgement was taken away from it.

Candidates are ranked and capped per sweep (`SWEEP_MAX_CANDIDATES`, default 25); the rest are
deferred to the next one.

An officer can also run a sweep on demand from the watch floor — the **Sweep 12 h** button —
which enqueues the same job on the same idempotency key, so a manual sweep and a scheduled one
cannot both run.

---

## Investigator — research

**The problem it solves.** An alert says *something happened*. It does not say who owns the
hull, who owns the owner, whether the flag changed three times in two years, or whether
anyone in that chain is sanctioned. That is the analyst's hour, and this is the agent that
spends it.

**Runs as two parallel branches**, each in its own AgentCore runtime session (a second
concurrent call to the same session returns 409):

| Branch | Question | Tools it reaches for |
|---|---|---|
| **identity** | Who is this hull, who owns it, is anyone sanctioned? | `registry.lookup_vessel`, `registry.ownership_network`, `registry.flag_history`, `registry.fleet_associations`, `registry.sanctions_screen` |
| **behaviour** | What did it actually do, in time order? | `ais.get_vessel_track`, `ais.get_latest_position`, `ais.find_vessels_near`, `ais.detect_loitering`, `ais.detect_rendezvous`, plus `geo.*` for context |

| Tool | Called when |
|---|---|
| `registry.lookup_vessel` | First, to establish name, IMO, flag and type |
| `registry.ownership_network` | When a registered owner exists and the chain needs walking |
| `registry.flag_history` | To test for flag-hopping |
| `registry.fleet_associations` | To place the hull in a fleet |
| `registry.sanctions_screen` | On every name in the ownership chain |
| `ais.get_vessel_track` | To reconstruct the window around the alert |
| `ais.find_vessels_near` | When a rendezvous or a meeting is plausible |
| `geo.nearest_ports`, `geo.point_in_zones` | To say whether "open water" is literally true |

| Action it can take | Effect |
|---|---|
| Return `InvestigationFindings` JSON | Structured findings: identity, ownership, sanctions exposure, behaviour summary, risk indicators, counter-indicators, confidence, evidence, information gaps |

It changes nothing in the database. Every claim carries the tool that produced it, cited as
`server.tool`; the orchestrator prefixes bare tool names from LangChain's MCP adapter so the
dotted form survives into the report.

---

## Tasking — collection planning

**The problem it solves.** When the evidence runs out, the honest next step is often to look
again with a satellite rather than to guess. Tasking decides whether that is worth doing and
writes the request.

| Step | Tool | Why, at that moment |
|---|---|---|
| 1 | `imagery.search_sentinel_scenes` | Is there already a scene over that water at that time? |
| 2 | `imagery.estimate_next_pass` | When would a satellite next be overhead? |
| 3 | `geo.distance_nm`, `geo.point_in_zones` | Sanity-check the area of interest against the vessel's last known position |
| 4 | `imagery.create_tasking_request` | Write the proposal |

| Action it can take | Effect | Guard |
|---|---|---|
| `imagery.create_tasking_request(…)` | Creates a tasking row with status **`proposed`** | `check_aoi()` rejects the proposal if the area is more than 200 nm from the vessel's last known position, the radius is outside 1–100 nm, the sensor is not one the imagery server accepts, or the window ends before it starts |

**It has no AIS tool.** The orchestrator derives the gap position in code (`gap_position`) and
hands it over. Given only prose, it once proposed a SAR collection over New York for a vessel
off Singapore — hence both the code-derived position and the distance check.

**Nothing is tasked.** A `proposed` row appears in the officer's Tasking queue with Approve
and Reject buttons. Approval is a separate officer endpoint; the agent cannot call it.

---

## Orchestrator — the graph

**Not a planner.** It is a state machine written in code (ADR-0003). No model chooses what
runs next, which is what makes a run reproducible and bounds its cost.

```
identity branch  ─┐
                  ├─ join ─ tasking ─ report ─ persist
behaviour branch ─┘
```

| Node | What happens | Model involved? |
|---|---|---|
| **fan-out** | Calls Investigator twice over A2A, in parallel, separate runtime sessions | no |
| **join** | Pure merge of the two findings; each field taken from the branch that owns it | no |
| **tasking** | Calls the Tasking agent with the code-derived gap position, then validates the AOI | no (the agent's own model runs inside) |
| **report** | One tool-less model call over validated JSON — never a chat transcript | **yes** |
| **policy check** | `validate_report` on allowed actions and evidence traceability; one redraft with the violations listed, then fail | no |
| **persist** | Writes the report, manifest and evidence snapshots | no |

| Tool / endpoint | Called when |
|---|---|
| `GET /vessels/{mmsi}/track` | Before tasking, to derive where the vessel went dark |
| `POST /investigations/{id}/progress` | At every node transition — this is what lights the progress panel |
| `POST /investigations/{id}/complete` | Once, with the finished report |
| `POST /investigations/{id}/fail` | If the graph cannot produce a valid report |

Confidence is capped in code when a line of enquiry fails (`cap_for_degraded`), so a
half-finished investigation can never present itself as a confident one. The manifest records
every model, prompt version, tool version and attempt.

---

## What agents may change

| Capability | Watch | Investigator | Tasking | Orchestrator | Officer |
|---|---|---|---|---|---|
| Raise an alert | ✅ draft | — | — | — | ✅ |
| Dismiss a candidate | ✅ (except definitional) | — | — | — | ✅ |
| Accept / reject an alert | — | — | — | — | ✅ **only** |
| Open an investigation | — | — | — | — | ✅ (the API also opens one automatically for `high` severity — `AUTO_INVESTIGATE_SEVERITIES`) |
| Write findings | — | ✅ | — | — | — |
| Propose a collection | — | — | ✅ `proposed` | — | — |
| **Approve a collection** | — | — | — | — | ✅ **only** |
| Complete an investigation | — | — | — | ✅ | — |
| Mark a finding reviewed | — | — | — | — | ✅ **only** |

Every row above is written to an append-only audit table by database trigger. A typical
investigation leaves this trail:

```
04:17Z  scheduler                  sweep.requested
04:18Z  argus-agent-watch          alert.raised          × 4
04:20Z  worker                     sweep.completed
04:21Z  officer@example.com        investigation.started
04:22Z  argus-agent-orchestrator   investigation.completed
04:23Z  tasking-agent              tasking.proposed
```

## How the agents are isolated

- Agents run in an isolated subnet group with **no route to the internet** (ADR-0007).
- The four MCP tool servers sit behind **one AgentCore Gateway** with a Cedar policy engine in
  ENFORCE mode: an agent cannot call a tool it was not granted, and the policies are generated
  from `mcp-servers/tools.json` so adding a tool without regenerating them blocks the call.
- Agents prove who they are with an STS-signed caller token; servers verify it and allow by
  IAM role name, fail-closed (`TOOL_ALLOWED_ROLES`).
- Agent-only API routes are listed explicitly; anything not on that list is closed to agents.
- A Bedrock Guardrail is attached to both frameworks through `model_spec()`.
