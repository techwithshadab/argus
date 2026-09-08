-- Review states for advisory findings, decision provenance for actions, and the append-only audit log.
-- Idempotent: runs on a fresh volume via docker-entrypoint and again from ais-replay on Aurora.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS reviewed_by  TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS reviewed_at  TIMESTAMPTZ;

ALTER TABLE investigations ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS reviewed_by  TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS reviewed_at  TIMESTAMPTZ;

ALTER TABLE tasking_requests ADD COLUMN IF NOT EXISTS decided_by TEXT;
ALTER TABLE tasking_requests ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS audit_events (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor        TEXT NOT NULL,                     -- IAM role name of an agent, or the watch officer's id
  actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('agent', 'watch_officer', 'system')),
  action       TEXT NOT NULL,                     -- alert.raised, alert.reviewed, investigation.started, ...
  entity_kind  TEXT NOT NULL,                     -- alert | investigation | tasking_request | sweep
  entity_id    TEXT,
  details      JSONB NOT NULL DEFAULT '{}'::jsonb,
  trace_id     TEXT,
  manifest_ref TEXT                               -- provenance manifest id (phase 4); trace id until then
);
CREATE INDEX IF NOT EXISTS audit_events_entity_idx ON audit_events (entity_kind, entity_id);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx ON audit_events (ts);

CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events;
CREATE TRIGGER audit_events_no_change
  BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
