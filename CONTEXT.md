# Argus

Maritime domain awareness (MDA) investigation system: agents watch vessel traffic, build evidence
against vessels behaving deceptively, and hand advisory findings and proposed actions to a human
watch officer. A single organisation runs one deployment.

## Language

### People and roles

**Watch Officer**:
The human who reviews alerts and reports and decides on actions. The only party that can approve an action. On AWS the officer is whoever Cognito signed in at the load balancer (the email claim); locally it is the typed id.
_Avoid_: User, operator, analyst

**Agent**:
An autonomous LLM-driven worker (Watch, Investigator, Tasking, Orchestrator) with its own identity and least-privilege access. Agents advise; they never approve.
_Avoid_: Bot, model, service

**Model tier**:
The class of model a step is allowed to use: fast, standard, or strong. Chosen by the step's judgement load, not by the Agent's name.
_Avoid_: Model size, provider

### Findings (advisory)

**Alert**:
A single evidenced anomaly (AIS gap, MMSI spoof, loitering, rendezvous, zone incursion) on one vessel, raised by the Watch agent for review. Advisory, never an action.
_Avoid_: Detection, event, hit

**Sweep**:
One Watch-agent review of a time window that may raise zero or more Alerts.
_Avoid_: Scan, run, poll

**Investigation**:
The case built for one vessel after a trigger (an Alert or a manual request), ending in a Vessel of Interest Report or a failure.
_Avoid_: Case, job, task

**Vessel of Interest Report** (VOI Report):
The Orchestrator's synthesis of an Investigation: headline, indicators traceable to Evidence, counter-indicators, gaps, confidence, and recommended actions for humans.
_Avoid_: Report (alone), assessment, summary

**Evidence**:
A tool output (source, summary, reference) that an Alert or VOI Report cites. Every indicator must trace to Evidence.
_Avoid_: Proof, data point

**Review state**:
The human disposition of an advisory finding: `draft` (written by an agent), `reviewed`, then `accepted` or `rejected`, with reviewer and time recorded. Applies to Alerts and VOI Reports. An unreviewed finding is never presented as a human conclusion.
_Avoid_: Status (overloaded), approved (reserved for actions)

### Actions (gated)

**Action**:
Anything with effect outside Argus: imagery tasking, sharing with a partner, a boarding recommendation. Always proposed by an agent and approved or rejected by a Watch Officer.
_Avoid_: Recommendation (that is the agent's text), decision

**Tasking Request**:
A proposed Action to collect imagery over an area and time window. States: `proposed`, `approved`, `rejected`.
_Avoid_: Collection request, task, order

**Investigation policy**:
The rule that decides which Alerts open an Investigation automatically and which wait for a Watch Officer.
_Avoid_: Escalation rules, routing

### Provenance

**Provenance manifest**:
The record of everything a VOI Report was produced with: prompts, models, tool versions, schema version, code revision, and the Evidence it cited. What an Investigation is reproduced against.
_Avoid_: Metadata, trace

**Audit event**:
An append-only record of one state change (who or which Agent did what, when, under which manifest).
_Avoid_: Log line, history

### Data

**Ownership network**:
The graph of vessels, companies, people, sanction listings, and flags, and the relationships between them (owns, operates, beneficially owns, listed on, rendezvoused with, reflagged from). The Investigator's main source for multi-hop questions.
_Avoid_: Registry graph, knowledge graph, entity map

**Evidence snapshot**:
The copy a finding keeps of what it cited (positions, registry record, ownership network, tool outputs) at the moment it was made, so it stays reproducible after raw data ages out.
_Avoid_: Attachment, cache, backup

**Ground truth**:
The injected anomalies of a synthetic scenario, used only to score Sweeps. Never exposed to Agents.
_Avoid_: Labels, answers

**Scenario clock**:
The fixed "now" of a replayed scenario, which every Agent and the API use instead of wall-clock time.
_Avoid_: Current time, now
