-- Scenario metadata shared between the replay task and the API (they run on different hosts on AWS).
CREATE TABLE IF NOT EXISTS scenario_meta (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
