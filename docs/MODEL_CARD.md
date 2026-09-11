# Argus Model Card

What the system does, what it is allowed to decide, where it fails, and what a person
must do. Written for the officer who acts on its output and the reviewer who has to
judge whether it should be deployed at all.

Last reviewed: 9 September 2026, against the code in this repository.

## What This Is

Argus watches maritime AIS traffic for behaviour that suggests a vessel is hiding
something, and assembles the evidence for a human watch officer. It is a decision
support system. It does not decide anything.

Four agents, each with a narrow job:

| Agent | Framework | Model tier | What it produces |
|---|---|---|---|
| Watch | Strands | fast (Nova Lite) | Alerts from detector candidates, with a severity and a reason |
| Investigator | LangGraph | strong (Nova Pro) | Findings on identity, ownership, sanctions exposure and behaviour |
| Tasking | Strands | standard (Nova 2 Lite) | A proposed satellite or aircraft collection, for approval |
| Orchestrator | code, not a model | strong for its report node only | The Vessel of Interest report |

The orchestrator is a code graph. No model chooses the sequence of steps, which agent
runs, or whether a case proceeds. The only model call in it writes the final report from
already validated JSON.

## Intended Use

A maritime watch floor: a small number of trained officers reviewing anomalies in a
defined sea area, deciding which are worth attention and which collection assets to
point at them. Every output is a draft until an officer accepts it.

## Out Of Scope

These are not cautions. They are uses the system must not be put to.

- **Automated enforcement of any kind.** Nothing here is evidence in a legal sense, and
  no output should trigger an interdiction, a detention, a boarding, or a sanction
  without independent human investigation.
- **Tracking a person.** The subject is a vessel. Beneficial owner names exist only as
  ciphertext, are read by one tool, and never reach the user interface or the report.
- **A source of truth about identity.** An AIS identity is self-reported and trivially
  falsified. That is precisely what several detectors look for.
- **Operation without a trained officer.** The review step is not a formality. It is the
  control that makes the rest of the design acceptable.

## How It Decides

The parts that matter are deliberately not left to a model.

**Detection is deterministic.** Five detectors run as SQL over PostGIS: AIS gaps, MMSI
conflicts, loitering, rendezvous, zone incursions. They produce candidates with fixed
vessel identifiers, time windows and evidence. The Watch model judges those candidates.
It never supplies a vessel identifier or a window, because when it did, recall swung
between 0.4 and 0.8 between runs on the same data.

**Some findings cannot be dismissed by a model.** One identity reporting from two places
at once, and two vessels stopped together outside any declared anchorage, can only be
raised. The model was dismissing the second as "normal operations" about half the time.

**A report cannot claim more than its evidence.** Every indicator must trace to a tool
result. Recommended actions must match an allowed pattern. A failed line of enquiry caps
the report's confidence and priority in code, and says so in the report.

## Known Failure Modes

Observed in this deployment, not hypothetical.

| Failure | What it looks like | What limits the damage |
|---|---|---|
| Model invents coordinates | A collection proposed over New York for a vessel off Singapore | The position is resolved in code and an area more than 200 nautical miles from the vessel is rejected |
| Model echoes a detector name as an alert kind | `mmsi_conflict` instead of `mmsi_spoof` | Alias mapping in the tool and the API validator |
| Half an investigation reads as a whole one | Fluent, confident report on one line of enquiry | Confidence and priority capped, the missing branch named in the gaps |
| Guardrail blocks a legitimate report | A report describing suspected smuggling trips the misconduct filter | Filters calibrated low on purpose, one retry with plainer wording, the block recorded |
| Duplicate alerts flood the queue | 131 alerts awaiting review after one day | Unique index on the window, dedup against every non-dismissed alert, stale drafts expire |
| Feed goes silent | No positions, task still healthy | The ingest task publishes its own heartbeat; the alarm treats silence as breaching |

## Data

**In:** AIS positions (public, live via AISStream or a replayed synthetic scenario), a
vessel registry, a sanctions list (local or OpenSanctions), Sentinel scene metadata,
OpenStreetMap geography.

**Personal data:** beneficial owner names and person entity names, stored only as
pgcrypto ciphertext under a key in Secrets Manager. One tool decrypts them. The API,
the reports and the user interface stay pseudonymous.

**Retention:** positions 90 days hot then archived as Parquet; findings, evidence
snapshots and the audit log for seven years. The audit log is append-only by database
trigger and is never pruned.

## Evaluation

Detector recall and precision are scored against labelled synthetic scenarios on every
change, in continuous integration, with no model involved. Model-in-the-loop evaluations
score each agent against floors in `evals/thresholds.yaml`; a prompt or model change is
not finished until that gate passes. An alarm fires when a gate misses a floor, and also
when no gate has reported at all.

Scores are not a safety argument. They measure whether a change made things worse on
scenarios we wrote, which is a narrower claim than it sounds.

## Provenance

Every investigation stores a manifest: code revision, prompt hashes and versions, the
model and tier for each node, attempts and escalations, tool versions, and the
identifiers of the evidence snapshots. Evidence is frozen at the time of the finding, so
a report can be re-read years later against what was actually known then, not against
what the database says today.

## Human Oversight

Agents propose. Officers decide. This is enforced in the API, not by convention: agent
roles and the operator role are separate allowlists, and an agent role calling a review
or approval route is refused (ADR-0019). Every decision records who made it and when, in
an append-only log.

## Environmental And Cost Note

The deployment idles at roughly $400 a month. Trace indexing is sampled rather than
complete, and a monthly budget notifies before the spend lands rather than after.
