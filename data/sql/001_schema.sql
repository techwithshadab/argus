-- Maritime Domain Awareness demo schema (PostGIS)
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS vessels (
  mmsi         BIGINT PRIMARY KEY,
  imo          BIGINT,
  name         TEXT,
  callsign     TEXT,
  flag         TEXT,
  ship_type    TEXT,
  length_m     REAL,
  is_synthetic BOOLEAN NOT NULL DEFAULT TRUE,
  meta         JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS positions (
  id          BIGSERIAL PRIMARY KEY,
  mmsi        BIGINT NOT NULL,
  ts          TIMESTAMPTZ NOT NULL,
  geom        GEOGRAPHY(POINT, 4326) NOT NULL,
  sog         REAL,
  cog         REAL,
  heading     REAL,
  nav_status  TEXT,
  source      TEXT NOT NULL DEFAULT 'synthetic'
);
CREATE INDEX IF NOT EXISTS positions_ts_idx ON positions (ts);
CREATE INDEX IF NOT EXISTS positions_geom_idx ON positions USING GIST (geom);

CREATE TABLE IF NOT EXISTS zones (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,
  geom        GEOGRAPHY(POLYGON, 4326) NOT NULL,
  properties  JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS zones_geom_idx ON zones USING GIST (geom);

CREATE TABLE IF NOT EXISTS registry (
  mmsi             BIGINT PRIMARY KEY,
  imo              BIGINT,
  name             TEXT,
  flag             TEXT,
  flag_history     JSONB DEFAULT '[]'::jsonb,
  registered_owner TEXT,
  operator         TEXT,
  beneficial_owner TEXT,
  sanctions        JSONB DEFAULT '[]'::jsonb,
  fleet            JSONB DEFAULT '[]'::jsonb,
  notes            TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  mmsi        BIGINT NOT NULL,
  kind        TEXT NOT NULL,
  severity    TEXT NOT NULL,
  score       REAL NOT NULL,
  started_at  TIMESTAMPTZ,
  ended_at    TIMESTAMPTZ,
  details     JSONB DEFAULT '{}'::jsonb,
  status      TEXT NOT NULL DEFAULT 'open',
  created_by  TEXT NOT NULL DEFAULT 'watch-agent',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alerts_mmsi_idx ON alerts (mmsi);

CREATE TABLE IF NOT EXISTS investigations (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  mmsi        BIGINT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'running',
  trigger     TEXT,
  report      JSONB,
  trace_id    TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasking_requests (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  mmsi         BIGINT,
  sensor       TEXT NOT NULL,
  aoi          GEOGRAPHY(POLYGON, 4326),
  window_start TIMESTAMPTZ,
  window_end   TIMESTAMPTZ,
  rationale    TEXT,
  priority     TEXT DEFAULT 'routine',
  status       TEXT NOT NULL DEFAULT 'proposed',
  created_by   TEXT NOT NULL DEFAULT 'tasking-agent',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW latest_positions AS
SELECT DISTINCT ON (p.mmsi)
  p.mmsi, v.name, v.flag, v.ship_type, p.ts,
  ST_Y(p.geom::geometry) AS lat, ST_X(p.geom::geometry) AS lon,
  p.sog, p.cog, p.nav_status
FROM positions p LEFT JOIN vessels v USING (mmsi)
ORDER BY p.mmsi, p.ts DESC;
