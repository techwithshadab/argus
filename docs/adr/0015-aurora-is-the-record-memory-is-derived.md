# ADR-0015: Aurora is the record; AgentCore Memory is derived context

Date: 2026-09-07. Status: accepted.

## Context

Investigations are stored twice: the report, manifest and evidence snapshots in Aurora
(`investigations`, `evidence_snapshots`, the append-only `audit_events`) and a summary of
the run in AgentCore Memory so a repeat investigation of the same hull sees the last
one. The orchestrator wrote Memory before posting `/complete`, so a failure between the
two left a Memory entry for an investigation the database recorded as failed, and the
question "which store is right?" had no written answer.

## Decision

- Aurora is the system of record for findings, reports, reviews, actions and audit.
  Memory holds prompt context only: what the last runs concluded, for the next run's
  prior-context section (`VesselMemory.recall`).
- The orchestrator persists to Aurora first and writes Memory after `/complete` returns
  (`persist()` in `agents/orchestrator/app.py`). A Memory write failure is logged and
  never fails the investigation; a database failure means no Memory entry.
- Memory is rebuildable from Aurora and may be cleared at any time; nothing reads it for
  a decision, a review or an audit answer.
- Long-term extraction lags by minutes, so recall also reads the raw events of the last
  sessions; that read is context too, not record.

## Consequences

Losing the Memory store loses nothing an officer signed. The prior-context section can
be stale by one run when Memory is behind, which the report's caveats do not need to
mention because the record itself is complete.
