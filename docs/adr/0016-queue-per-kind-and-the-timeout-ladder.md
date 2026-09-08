# ADR-0016: A queue per job kind, one timeout ladder, and where checkpointing goes

Date: 2026-09-07. Status: accepted (extends ADR-0008).

## Context

ADR-0008 made the database row the job and SQS the wake-up. One queue and one worker
loop carried sweeps and investigations with a single visibility timeout; a scheduled
sweep could wait behind twenty-five auto-opened investigations; a redelivery at the
visibility timeout met a row still `running` and was acknowledged, so a stuck job ran
to the reaper's deadline with nobody holding the message; and a worker restart lost the
in-flight LangGraph state.

## Decision

- **One queue per kind.** `argus-sweeps` and `argus-jobs` (investigations), each with
  its dead-letter queue, each drained by its own ECS service (`WORKER_QUEUE_KIND`),
  sized for the kind (one sweep worker, four investigation workers). The scheduler
  targets the sweeps queue with its own dead-letter queue and retry policy.
- **The timeout ladder** per kind: node timeout × graph depth < job `timeout_s` <
  queue visibility < `timeout_s` + reaper grace (`ladder_ok` in `jobqueue.py`, pinned by
  a unit test). Visibility is extended by a heartbeat while the handler runs, so a
  redelivery only happens when a worker is gone; a redelivered message that finds a
  `running` row past its timeout reclaims it (`reclaim`) instead of acknowledging it.
- **Checkpointing is a roadmap item, not a patch.** A worker restart still loses the
  in-flight graph state; the job is reclaimed and restarted from the beginning, which
  is correct because handlers are idempotent. A LangGraph checkpointer in Aurora and an
  asynchronous hand-off to the orchestrator (the worker enqueues, the orchestrator posts
  progress and completion, which it already does) are the next step in
  `docs/ROADMAP.md`; both fit this ladder without changing it.

## Consequences

Sweeps never queue behind investigations, a stuck job is retried by another worker
inside its own deadline, and the alarms `argus-*-oldest-message` and `argus-*-backlog`
have a meaning per kind. The cost is a second worker service (one small task).
