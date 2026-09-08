---
status: accepted
---
# Positions are day-partitioned with tiered retention; findings snapshot the evidence they cite

Live AIS produces millions of rows a day and the positions table was a single unpartitioned table with a serial key and no retention. Decision: partition positions by day on `ts` (pg_partman, available on Aurora PostgreSQL), primary key `(mmsi, ts)`, GIST index per partition; keep 90 days hot and archive older partitions to Parquet on S3. Because raw rows age out, every Alert and VOI Report stores a snapshot of the evidence it cites (the positions, registry state and tool outputs) so a report is reproducible after the source rows are gone.

## Considered options

- TimescaleDB: better ergonomics but unavailable on Aurora, forcing self-managed Postgres.
- Single table: works for the replayed scenario only.

## As built (phase 2)

Native range partitioning with a SQL maintenance function (`positions_ensure_partitions`) instead of pg_partman, so one script runs on the local PostGIS image and on Aurora; an idempotent conversion migrates a plain table in place. **No primary key on positions**: AIS carries several reports per (mmsi, ts) and a spoofed identity is two transmitters sharing one MMSI, and the first attempt at a (mmsi, ts) key silently dropped the spoof evidence. Retention is `retention_policy` as data plus `positions_expired_partitions` / `positions_drop_partition`; the archiver (`services/archiver`, an ECS scheduled task on AWS, `docker compose run archiver` locally) exports each expired day to Parquet, verifies the object, drops the partition and writes an audit event. Evidence snapshots (`evidence_snapshots`) are captured by the API when an alert is raised (positions around the window plus the cited tool outputs) and when an investigation completes (24 h of positions, the registry record, the depth-2 ownership network, and every evidence item in the report).

## Consequences

Evidence has a storage cost per finding, accepted so that the audit story does not depend on retention. Detectors must be partition-aware (window queries bounded by `ts`) to keep sweeps fast.
