# ADR-0022: Detectors decide what is anomalous; the model judges what it means

Date: 2026-09-09. Status: accepted.

## Context

The Watch agent was given tools and a prompt and asked to find anomalies. It produced
alerts, and the alerts were unreliable in a way that was hard to see: recall on the same
scenario swung between 0.4 and 0.8 run to run. The failure was not that the model was bad
at judgement. It was that the model was being asked to do arithmetic and bookkeeping.

Specifically, Nova Lite would copy one vessel's gap window onto another vessel, so an
alert would name a real anomaly with the wrong identity attached. It would also dismiss
the scenario's at-sea transfer as "normal operations" about one run in two. An evaluation
scorer that matches alerts to ground truth by time overlap scores a wrong window as a
miss, so the same behaviour that made the alert useless to an officer also made the score
meaningless.

## Decision

Split the sweep into a deterministic pre-pass and a judgement pass.

**The pre-pass is SQL.** Five detectors run through a short-lived MCP client: AIS gaps,
MMSI conflicts, loitering, rendezvous, zone incursions. `agents/shared/sweep.py` turns
their rows into candidates with a fixed vessel identifier, kind, time window and evidence,
ranks them, and caps how many one sweep carries (`SWEEP_MAX_CANDIDATES`, default 25).
It is pure and unit-tested.

**The model judges candidates, and only candidates.** `raise_alert` and
`dismiss_candidate` take a candidate identifier. The model supplies severity, a score and
a rationale. It cannot supply a vessel identifier or a window, because those come from the
detector row.

**Some candidates cannot be dismissed at all.** `non_dismissible()` names them: one MMSI
transmitting from two places, and a rendezvous outside any declared anchorage or port.
These are definitions rather than judgements, so they are code. A model may raise them
with a lower severity; it may not wave them away.

## Consequences

Recall stopped swinging. The sweep is reproducible in its detection half, which means an
evaluation score measures the judgement half, which is the part a prompt or model change
actually affects.

Adding a detector is a SQL and `sweep.py` change with a unit test, not a prompt edit. The
cap means a busy area defers candidates to the next sweep rather than exhausting the
model's context; deferred counts are reported in the sweep summary.

The cost is that Argus can only detect what someone has written a detector for. A model
free to look for anything might find something we did not anticipate. On a watch floor,
we judged a system that reliably finds five things more useful than one that
unreliably finds six.
