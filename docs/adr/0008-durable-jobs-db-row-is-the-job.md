---
status: accepted
---
# Sweeps and investigations are durable jobs; the database row is the job

Investigations used to run as FastAPI background tasks: a restarted API container silently lost them, nothing retried, and the UI polled a spinner. Decision: every sweep and investigation is a row in `jobs` (status, attempts, timeout, progress, idempotency key) and the queue carries only the job id. Redis Streams with a consumer group locally, SQS with a dead-letter queue on AWS; both are at-least-once, which is safe because a worker claims the row with a conditional update before running it and every handler is idempotent. Retries use exponential backoff for transient failures only (connectivity, 5xx, throttling); anything else fails fast. Progress events go to a Redis stream that the API forwards as server-sent events, so the UI shows which specialist is running instead of a timer.

Sweeps call the Watch agent directly over A2A from the worker; the orchestrator is no longer a relay. Scheduled sweeps come from EventBridge Scheduler on AWS and from the worker's own timer locally, both on the same interval-bucket idempotency key, so a double firing is a no-op. An investigation policy opens an investigation automatically for alerts whose severity is in `AUTO_INVESTIGATE_SEVERITIES` (default `high`), keyed by alert id so the same alert never opens two.

## Considered options

- Step Functions: retries and timeouts for free, but the workflow definition would live outside the code graph (ADR-0003) and only exist on AWS.
- Keep background tasks: simplest; unacceptable once sweeps run unattended.

## Consequences

The worker is a second process from the API image; scaling investigations is scaling workers. Dead jobs are visible in `jobs` (status `dead`), in the DLQ, and in the audit log. Job rows are retained 90 days; the audit log keeps the outcome for seven years.
