-- Alerts stopped duplicating and started expiring (gap audit P5).
--
-- One day of live sweeps left 131 alerts awaiting review. Two causes: the sweep only
-- checked `open` alerts before raising, so a window already investigated or rejected
-- came back on the next sweep, and nothing ever closed a draft that no officer had
-- looked at. The review queue is the product; a queue nobody can finish is the same as
-- no queue. Idempotent, like every file here.

-- ---------------------------------------------------------------- no exact duplicates
-- The same vessel, the same anomaly kind and the same start is one alert, however many
-- times a sweep sees it. Partial, so a dismissed alert does not block a genuine
-- re-occurrence of the same window later. `started_at` is nullable in the schema but
-- required by the raise path, so the index covers the rows that matter.
--
-- The duplicates this index forbids are exactly what a running deployment already has:
-- creating it on the live database failed with a UniqueViolation and crash-looped the
-- ingest task, because this file is re-applied on every start. So the existing ones are
-- folded first: the oldest row of each group is kept (it is the one officers have been
-- looking at, and the one any investigation references), and the rest are dismissed
-- with a reason rather than deleted, because `alerts` is referenced by
-- `investigations.alert_id` and deleting a row would orphan a case. Idempotent: after
-- the first run there is nothing left to fold.
UPDATE alerts a
   SET status = 'dismissed',
       details = coalesce(a.details, '{}'::jsonb)
                 || jsonb_build_object('dismissed_reason',
                      'duplicate of an earlier alert for the same vessel, kind and window')
 WHERE a.status <> 'dismissed'
   AND a.started_at IS NOT NULL
   AND EXISTS (
         SELECT 1 FROM alerts b
          WHERE b.mmsi = a.mmsi AND b.kind = a.kind AND b.started_at = a.started_at
            AND b.status <> 'dismissed'
            AND (b.created_at, b.id) < (a.created_at, a.id)
       );

CREATE UNIQUE INDEX IF NOT EXISTS alerts_open_window_uniq
  ON alerts (mmsi, kind, started_at)
  WHERE status <> 'dismissed' AND started_at IS NOT NULL;

-- ---------------------------------------------------------------- ageing out
-- A draft nobody reviewed within the window is closed, with the reason recorded. Not a
-- deletion: the alert stays readable and auditable, it simply leaves the queue.
CREATE OR REPLACE FUNCTION expire_stale_alerts(max_age_hours INT DEFAULT 72)
RETURNS INT AS $$
DECLARE
  expired INT;
BEGIN
  WITH stale AS (
    UPDATE alerts
       SET status = 'expired',
           details = coalesce(details, '{}'::jsonb)
                     || jsonb_build_object('expired_reason',
                          format('no officer review within %s hours', max_age_hours))
     WHERE status = 'open'
       AND review_state = 'draft'
       AND created_at < now() - make_interval(hours => max_age_hours)
    RETURNING id, mmsi, kind
  ), logged AS (
    INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details)
    SELECT 'retention', 'system', 'alert.expired', 'alert', id::text,
           jsonb_build_object('mmsi', mmsi, 'kind', kind, 'max_age_hours', max_age_hours)
      FROM stale
    RETURNING 1
  )
  SELECT count(*) INTO expired FROM logged;
  RETURN expired;
END;
$$ LANGUAGE plpgsql;

-- The list routes order by this; without it every page scan reads the whole table once
-- the alert count grows.
CREATE INDEX IF NOT EXISTS alerts_created_at_idx ON alerts (created_at DESC);
