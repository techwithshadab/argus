-- Phase 2 data model (ADR-0005): day-partitioned positions with a retention function, evidence
-- snapshots so findings stay reproducible after raw rows age out, encrypted personal data, and the
-- retention policy as data. Idempotent: safe to re-apply on every start.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- evidence snapshots
CREATE TABLE IF NOT EXISTS evidence_snapshots (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  entity_kind  TEXT NOT NULL CHECK (entity_kind IN ('alert', 'investigation')),
  entity_id    UUID NOT NULL,
  mmsi         BIGINT,
  kind         TEXT NOT NULL,                       -- positions | registry | ownership_network | tool_output
  source       TEXT,
  reference    TEXT,
  summary      TEXT,
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_snapshots_entity_idx ON evidence_snapshots (entity_kind, entity_id);

-- ---------------------------------------------------------------- personal data
-- beneficial_owner may name a person. The encrypted copy is authoritative; the plaintext column is
-- cleared by the loader once the encrypted value is written.
ALTER TABLE registry ADD COLUMN IF NOT EXISTS beneficial_owner_enc BYTEA;

-- ---------------------------------------------------------------- retention policy as data
CREATE TABLE IF NOT EXISTS retention_policy (
  class     TEXT PRIMARY KEY,
  hot_days  INT,                                     -- NULL = keep in the primary store indefinitely
  archive   TEXT NOT NULL,                           -- parquet | keep
  note      TEXT
);
INSERT INTO retention_policy (class, hot_days, archive, note) VALUES
  ('positions',          90,   'parquet', 'Raw AIS. Daily partitions older than hot_days are exported to Parquet and dropped.'),
  ('alerts',             NULL, 'keep',    'Advisory findings with review state. Kept.'),
  ('investigations',     2555, 'keep',    'VOI reports and provenance. Seven years.'),
  ('evidence_snapshots', 2555, 'keep',    'Evidence cited by findings. Lives as long as the finding.'),
  ('audit_events',       2555, 'keep',    'Append-only. Seven years.'),
  ('tasking_requests',   2555, 'keep',    'Actions and who decided them.')
ON CONFLICT (class) DO NOTHING;

-- ---------------------------------------------------------------- positions: daily partitions
-- Native range partitioning with a small maintenance function; no pg_partman dependency, so the same
-- SQL runs on the local PostGIS image and on Aurora.
CREATE OR REPLACE FUNCTION positions_ensure_partitions(d_from DATE, d_to DATE)
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE d DATE; part TEXT; made INT := 0;
BEGIN
  IF d_from IS NULL OR d_to IS NULL THEN RETURN 0; END IF;
  d := d_from;
  WHILE d <= d_to LOOP
    part := 'positions_p' || to_char(d, 'YYYYMMDD');
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part) THEN
      EXECUTE format('CREATE TABLE %I (LIKE positions INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', part);
      -- rows for that day may already sit in the default partition; move them before attaching
      EXECUTE format('WITH moved AS (DELETE FROM positions_default WHERE ts >= %L AND ts < %L RETURNING *) INSERT INTO %I SELECT * FROM moved',
                     d, d + 1, part);
      EXECUTE format('ALTER TABLE positions ATTACH PARTITION %I FOR VALUES FROM (%L) TO (%L)', part, d, d + 1);
      made := made + 1;
    END IF;
    d := d + 1;
  END LOOP;
  RETURN made;
END $$;

-- One-time conversion of the plain table created by 001_schema.sql. Guarded, so re-applying is a no-op.
DO $$
DECLARE lo DATE; hi DATE;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_partitioned_table pt JOIN pg_class c ON c.oid = pt.partrelid WHERE c.relname = 'positions') THEN
    RETURN;
  END IF;
  DROP VIEW IF EXISTS latest_positions;
  ALTER TABLE positions RENAME TO positions_legacy;
  ALTER INDEX IF EXISTS positions_mmsi_ts_idx RENAME TO positions_legacy_mmsi_ts_idx;
  ALTER INDEX IF EXISTS positions_ts_idx RENAME TO positions_legacy_ts_idx;
  ALTER INDEX IF EXISTS positions_geom_idx RENAME TO positions_legacy_geom_idx;
  CREATE TABLE positions (
    mmsi        BIGINT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    geom        GEOGRAPHY(POINT, 4326) NOT NULL,
    sog         REAL,
    cog         REAL,
    heading     REAL,
    nav_status  TEXT,
    source      TEXT NOT NULL DEFAULT 'synthetic'
  ) PARTITION BY RANGE (ts);
  -- No primary key on purpose: AIS legitimately carries several reports per (mmsi, ts), and a
  -- spoofed identity is two transmitters sharing one MMSI. Collapsing them would erase the evidence.
  CREATE TABLE positions_default PARTITION OF positions DEFAULT;
  CREATE INDEX positions_mmsi_ts_idx ON positions (mmsi, ts);
  CREATE INDEX positions_ts_idx ON positions (ts);
  CREATE INDEX positions_geom_idx ON positions USING GIST (geom);
  SELECT min(ts)::date, max(ts)::date INTO lo, hi FROM positions_legacy;
  PERFORM positions_ensure_partitions(lo, hi);
  INSERT INTO positions (mmsi, ts, geom, sog, cog, heading, nav_status, source)
    SELECT mmsi, ts, geom, sog, cog, heading, nav_status, source FROM positions_legacy;
  DROP TABLE positions_legacy;
END $$;

-- Databases converted by an earlier revision of this file carried a (mmsi, ts) primary key. Remove it
-- whatever Postgres named it (a rename during conversion can leave it as positions_pkey1).
DO $$
DECLARE c TEXT;
BEGIN
  SELECT conname INTO c FROM pg_constraint WHERE conrelid = 'positions'::regclass AND contype = 'p';
  IF c IS NOT NULL THEN EXECUTE format('ALTER TABLE positions DROP CONSTRAINT %I', c); END IF;
END $$;
CREATE INDEX IF NOT EXISTS positions_mmsi_ts_idx ON positions (mmsi, ts);

CREATE OR REPLACE VIEW latest_positions AS
SELECT DISTINCT ON (p.mmsi)
  p.mmsi, v.name, v.flag, v.ship_type, p.ts,
  ST_Y(p.geom::geometry) AS lat, ST_X(p.geom::geometry) AS lon,
  p.sog, p.cog, p.nav_status
FROM positions p LEFT JOIN vessels v USING (mmsi)
ORDER BY p.mmsi, p.ts DESC;

-- Partitions with their day and row estimate, for the archiver and for operators.
CREATE OR REPLACE FUNCTION positions_partitions()
RETURNS TABLE (partition_name TEXT, day DATE, row_estimate BIGINT) LANGUAGE sql STABLE AS $$
  SELECT c.relname::text,
         to_date(substring(c.relname from 'positions_p(\d{8})'), 'YYYYMMDD'),
         c.reltuples::bigint
  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
  WHERE p.relname = 'positions' AND c.relname ~ '^positions_p\d{8}$'
  ORDER BY 2;
$$;

-- Partitions that have fallen out of the hot window. The archiver exports each one to Parquet, then
-- calls positions_drop_partition. Nothing here deletes data on its own.
CREATE OR REPLACE FUNCTION positions_expired_partitions(p_hot_days INT DEFAULT NULL, p_now DATE DEFAULT current_date)
RETURNS TABLE (partition_name TEXT, day DATE, row_estimate BIGINT) LANGUAGE sql STABLE AS $$
  SELECT * FROM positions_partitions()
  WHERE day < p_now - coalesce(p_hot_days, (SELECT hot_days FROM retention_policy WHERE class = 'positions'), 90);
$$;

CREATE OR REPLACE FUNCTION positions_drop_partition(p_name TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
  IF p_name !~ '^positions_p\d{8}$' THEN RAISE EXCEPTION 'not a positions partition: %', p_name; END IF;
  EXECUTE format('ALTER TABLE positions DETACH PARTITION %I', p_name);
  EXECUTE format('DROP TABLE %I', p_name);
END $$;
