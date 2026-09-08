-- Phase 5: eval results as data, so the SLO on eval recall reads the latest run. Idempotent.
CREATE TABLE IF NOT EXISTS eval_runs (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  suite      TEXT NOT NULL,                 -- watch | investigator | tasking | report | e2e | detectors
  scenario   TEXT,
  scores     JSONB NOT NULL DEFAULT '{}'::jsonb,
  passed     BOOLEAN,
  thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
  code_revision TEXT,
  details    JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS eval_runs_suite_ts_idx ON eval_runs (suite, ts DESC);
INSERT INTO retention_policy (class, hot_days, archive, note) VALUES ('eval_runs', NULL, 'keep', 'Eval history; the regression baseline.') ON CONFLICT (class) DO NOTHING;
