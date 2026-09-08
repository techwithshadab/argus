-- Phase 3: durable jobs. The database is the source of truth for a job's state; the queue
-- (Redis Streams locally, SQS on AWS) only carries the job id. Idempotent.

CREATE TABLE IF NOT EXISTS jobs (
  id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  kind             TEXT NOT NULL CHECK (kind IN ('sweep', 'investigation')),
  idempotency_key  TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
  payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  result           JSONB,
  error            TEXT,
  attempts         INT NOT NULL DEFAULT 0,
  max_attempts     INT NOT NULL DEFAULT 3,
  timeout_s        INT NOT NULL DEFAULT 900,
  not_before       TIMESTAMPTZ,                          -- retry backoff
  progress         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{ts, step, status, detail}]
  requested_by     TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ,
  finished_at      TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One active job per (kind, key): enqueueing the same work twice returns the existing job.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_key_idx ON jobs (kind, idempotency_key) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, created_at);

ALTER TABLE investigations ADD COLUMN IF NOT EXISTS job_id UUID;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS alert_id UUID;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS requested_by TEXT;

INSERT INTO retention_policy (class, hot_days, archive, note)
  VALUES ('jobs', 90, 'keep', 'Job records; the audit log keeps the outcome for longer.')
ON CONFLICT (class) DO NOTHING;
