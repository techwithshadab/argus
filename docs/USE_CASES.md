# Argus use cases

Actors: **Watch Officer** (reviews findings, approves actions), **Analyst** (reads reports and evidence, tunes scenarios and prompts), **Operator** (deploys, stops, monitors, archives), and the **Agents** (Watch, Investigator, Tasking, Orchestrator), which advise and never approve.

## UC-1 Continuous watch of an area of interest

**Goal.** Vessel traffic in the area is reviewed on a schedule and every deceptive behaviour is raised as an evidenced alert.

**Flow.** EventBridge Scheduler (AWS) or the worker's timer (local) enqueues a sweep on an interval-bucket key → the worker calls the Watch agent over A2A → the Watch agent calls `list_open_alerts`, then the detectors (`find_ais_gaps`, `detect_mmsi_conflicts`, `detect_loitering`, `detect_rendezvous`, `list_zone_incursions`), gathers context (`point_in_zones`, vessel type) and raises alerts with `raise_alert` → alerts appear on the watch floor as **AI draft** with severity, window, rationale and evidence.

**Outcome.** Alerts with review state `draft`; a job record with progress; an audit event per alert raised. Duplicate sweeps are impossible (idempotency key). A stalled feed is visible in the header.

## UC-2 Automatic investigation of a severe alert

**Goal.** A high-severity alert opens an investigation without waiting for a human, so evidence is gathered while it is fresh.

**Flow.** `POST /alerts` with severity in `AUTO_INVESTIGATE_SEVERITIES` → the API creates the investigation and a job keyed by the alert id (a second request is deduplicated) → the worker invokes the orchestrator graph → the report lands as an **AI draft**.

**Outcome.** Investigation with `requested_by = policy`, audit events `investigation.auto_opened` and `investigation.started`, node-level progress in the UI.

## UC-3 Investigate a vessel (the graph)

**Goal.** Build the case an intelligence analyst would want before recommending action.

![Argus investigation graph: three triggers into one durable job; the orchestrator as a graph in code with two Investigator branches in parallel, a pure merge, the Tasking agent, the report writer and its policy and schema check with one correction loop, persistence of report, snapshots and manifest, and vessel memory feeding the next run; then the watch floor where officers review the draft, approve the proposal, and every decision lands in the audit log](diagrams/investigation-graph.png)

**Flow.**
1. Two Investigator branches run in parallel: *identity* (registry, `ownership_network`, `sanctions_screen`, `flag_history`) and *behaviour* (`get_vessel_track`, detectors, `point_in_zones`, `nearest_ports`, `find_vessels_near`).
2. The orchestrator merges them deterministically (owning branch wins each field, lists unioned, confidence the lower).
3. The Tasking agent gets the extracted evidence gap and decides whether imagery would materially reduce uncertainty; if so it creates a `proposed` tasking request.
4. The report node writes the VOI report from the merged JSON only; the policy check enforces allowed actions and evidence traceability, with one retry.
5. The report, evidence snapshots and provenance manifest are persisted.

**Outcome.** A VOI report with headline, priority, confidence, timeline, indicators and counter-indicators, ownership and sanctions, recommended actions, collection plan, evidence, gaps and caveats. Reproducible months later from the snapshots and the manifest.

## UC-4 Review findings and approve actions

**Goal.** Nothing an agent writes is presented as a human conclusion; nothing with external effect happens without a person.

**Flow.** The watch officer sets their id, reviews an alert (Accept / Reject, `Shift+A` / `Shift+R`) and a report (Accept report / Reject report), and approves or rejects each proposed tasking request. Every decision records who and when and writes an audit event.

**Outcome.** `review_state` on alerts and investigations; `approved/rejected` with `decided_by` on tasking; a complete audit trail (`GET /audit`, Audit tab).

## UC-5 Understand a vessel's track

**Goal.** See what a vessel did, including when it went silent.

**Flow.** Track from an alert card or report → the last 24 h drawn with start and latest markers, hover details per report, and every silence over 20 minutes as a dashed red segment labelled with its duration and window; the map fits to the track; Esc clears.

## UC-6 Explore the ownership network

**Goal.** Answer multi-hop questions: who controls this vessel, is any party listed, what else do they control, who has it met at sea.

**Flow.** The Investigator calls `ownership_network(mmsi, depth)` (person names decrypted); the report view shows the same neighbourhood pseudonymously with the path to any sanction listing.

## UC-7 Reproduce and audit a finding

**Goal.** Months later, answer "why did Argus say this" and "with what".

**Flow.** `GET /evidence?entity_kind=investigation&entity_id=…` for the positions, registry record, network and tool outputs captured at completion; `investigations.manifest` for code revision, prompt hashes, per-node models, attempts and tool versions; `GET /audit` for who did what; the trace id for the spans.

## UC-8 Run and gate evaluations

**Goal.** Know before shipping a prompt or model change whether detection and reports got worse.

**Flow.** Detector evals run on every pull request (CI PostGIS service). `python evals/node_evals.py --api … --gate` runs Watch, Investigator, Tasking and report suites with floors; nightly and on prompt or model changes via the `evals` workflow; `evals/e2e.sh` loops scenarios. Results in `eval_runs`, files and CloudWatch.

## UC-9 Operate the deployment

**Goal.** One command to deploy, update, stop, start or delete; nothing created outside IaC.

**Flow.** `make deploy`, `make stop-aws`, `make start-aws`, `make destroy`. Alarms on SLO burn and cost page an SNS topic. The archiver keeps the hot window at 90 days. See `docs/RUNBOOK.md`.

## UC-10 Switch or tune models

**Goal.** Control cost and quality per step without touching code.

**Flow.** Tiers by role (`MODEL_TIER_<ROLE>`), models by tier (`MODEL_ID_<TIER>`), or pin one model (`MODEL_ID`). Escalation covers a weak first answer. Other providers are available for development and eval comparison only; AWS refuses them.

## Non-goals

Global AIS coverage, multi-tenancy, autonomous action of any kind, and real-time detection on every position (sweeps are periodic by design so the Watch agent's judgement is always in the loop).
