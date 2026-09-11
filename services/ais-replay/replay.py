"""AIS ingest service.

Modes (AIS_MODE env):
  replay  : generate the synthetic scenario, bulk-load the history into PostGIS, then
            stream the same positions onto a Redis stream at REPLAY_SPEED x real time so
            the map animates and the watch agent has something to react to.
  live    : subscribe to AISStream.io (free API key) for the scenario bbox and ingest real
            AIS. Synthetic anomaly injection is not applied in live mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

import feed_metrics
import psycopg
import redis.asyncio as redis
from aisstatic import static_rows, upsert_registry_sql, upsert_vessel_sql
from aisvalidate import position_record, position_rows, should_flush
from areas import load_catalogue, select_areas, subscription_boxes
from datakey import data_key
from feed_metrics import FeedHealth
from network import build_graph, rendezvous_edges
from psycopg.types.json import Jsonb

sys.path.insert(0, "/app")
from data.synthetic.generator import ScenarioGenerator  # noqa: E402

log = logging.getLogger("ais-replay")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


def _password() -> str:
    """PGPASSWORD, or the `password` field of the Secrets Manager secret named by
    PGPASSWORD_SECRET_ARN (AgentCore Runtime has no secret injection; ECS injects
    PGPASSWORD at task start and re-reads the secret after a rotation, see refresh_password)."""
    pw = os.getenv("PGPASSWORD")
    if pw:
        return pw
    arn = os.getenv("PGPASSWORD_SECRET_ARN")
    if not arn:
        raise RuntimeError("PGPASSWORD or PGPASSWORD_SECRET_ARN is required")
    import json

    import boto3

    raw = boto3.client(
        "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
    ).get_secret_value(SecretId=arn)["SecretString"]
    try:
        pw = json.loads(raw).get("password") or raw
    except ValueError:
        pw = raw
    os.environ["PGPASSWORD"] = pw
    return pw


def refresh_password() -> bool:
    """Re-read the secret after a connection failure; True when the password changed (the
    secret was rotated), so the caller rebuilds its pool. No secret ARN: nothing to do."""
    if not os.getenv("PGPASSWORD_SECRET_ARN"):
        return False
    old = os.environ.pop("PGPASSWORD", None)
    try:
        return _password() != old
    except Exception:
        if old:
            os.environ["PGPASSWORD"] = old
        raise


def db_url() -> str:
    """DATABASE_URL if set, else assembled from PGHOST/PGPORT/PGDATABASE/PGUSER and the password (env or secret)."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return f"postgresql://{os.environ['PGUSER']}:{_password()}@{os.environ['PGHOST']}:{os.getenv('PGPORT', '5432')}/{os.getenv('PGDATABASE', 'argus')}"


def connect(**kw):
    """psycopg.connect that survives a password rotation (one re-read of the secret)."""
    global DB_URL
    try:
        return psycopg.connect(DB_URL, **kw)
    except psycopg.OperationalError as e:
        if "password authentication failed" not in str(e) or not refresh_password():
            raise
        DB_URL = db_url()
        return psycopg.connect(DB_URL, **kw)


DB_URL = db_url()
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MODE = os.getenv("AIS_MODE", "replay")
SCENARIO = os.getenv("SCENARIO_FILE", "/app/data/scenarios/east_med_baseline.yaml")
SPEED = float(
    os.getenv("REPLAY_SPEED", "60")
)  # 60 = one scenario minute per real second
LOOP = os.getenv("REPLAY_LOOP", "true").lower() in ("1", "true", "yes")
LOOP_PAUSE_S = int(os.getenv("REPLAY_LOOP_PAUSE_S", "20"))
PRELOAD = os.getenv("PRELOAD_HISTORY", "true").lower() == "true"
STREAM = "ais:positions"
AREAS_FILE = os.getenv("AREAS_FILE", "/app/data/areas.yaml")
WATCH_AREAS = os.getenv(
    "WATCH_AREAS", "all"
)  # catalogue areas watched besides the scenario


def _wait_for_db() -> None:
    for _ in range(60):
        try:
            with connect() as c:
                c.execute("SELECT 1")
            return
        except Exception as e:  # noqa: BLE001
            log.info("waiting for db: %s", e)
            time.sleep(2)
    raise RuntimeError("database not reachable")


def apply_schema() -> None:
    """Idempotent: local Postgres already ran this via docker-entrypoint; Aurora on AWS has not."""
    import glob

    files = sorted(
        f
        for f in glob.glob("/app/data/sql/*.sql")
        if not f.split("/")[-1].startswith("000")
    )
    with connect() as c:
        for f in files:
            with open(f) as fh:
                c.execute(fh.read())
        c.commit()
    log.info("schema applied: %s", [f.split("/")[-1] for f in files])
    ensure_databases(os.getenv("EXTRA_DATABASES", ""))


def ensure_databases(names: str) -> None:
    """Create the sibling databases other services need on the same cluster (Grafana on
    AWS). CREATE DATABASE cannot run in a transaction."""
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if not wanted:
        return
    with connect(autocommit=True) as c:
        for name in wanted:
            if not c.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone():
                c.execute(f'CREATE DATABASE "{name}"')
                log.info("created database %s", name)


def load_reference(gen: ScenarioGenerator) -> None:
    with connect() as c, c.cursor() as cur:
        for v in gen.vessels():
            cur.execute(
                """INSERT INTO vessels (mmsi, imo, name, flag, ship_type, length_m)
                   VALUES (%(mmsi)s, %(imo)s, %(name)s, %(flag)s, %(ship_type)s, %(length_m)s)
                   ON CONFLICT (mmsi) DO UPDATE SET name=EXCLUDED.name, flag=EXCLUDED.flag""",
                v,
            )
        cur.execute("DELETE FROM zones")
        for z in gen.zones():
            ring = z["polygon"] + [z["polygon"][0]]
            wkt = "POLYGON((" + ",".join(f"{lon} {lat}" for lon, lat in ring) + "))"
            cur.execute(
                "INSERT INTO zones (name, kind, geom, properties) VALUES (%s, %s, ST_GeogFromText(%s), %s)",
                (z["name"], z["kind"], wkt, Jsonb(z.get("properties", {}))),
            )
        for r in gen.registry():
            cur.execute(
                """INSERT INTO registry (mmsi, imo, name, flag, flag_history, registered_owner, operator,
                                         beneficial_owner, sanctions, fleet, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (mmsi) DO UPDATE SET sanctions=EXCLUDED.sanctions, notes=EXCLUDED.notes""",
                (
                    r["mmsi"],
                    r.get("imo"),
                    r["name"],
                    r["flag"],
                    Jsonb(r.get("flag_history", [])),
                    r.get("registered_owner"),
                    r.get("operator"),
                    r.get("beneficial_owner"),
                    Jsonb(r.get("sanctions", [])),
                    Jsonb(r.get("fleet", [])),
                    r.get("notes"),
                ),
            )
        c.commit()
    log.info(
        "reference data loaded: %d vessels, %d zones, %d registry rows",
        len(gen.vessels()),
        len(gen.zones()),
        len(gen.registry()),
    )
    encrypt_personal_data()
    load_ownership_network(gen)


def encrypt_personal_data() -> None:
    """beneficial_owner may name a person: keep only the encrypted copy (004_positions_partitions_evidence.sql)."""
    with connect() as c:
        c.execute(
            """UPDATE registry SET beneficial_owner_enc = pgp_sym_encrypt(beneficial_owner, %s), beneficial_owner = NULL
               WHERE beneficial_owner IS NOT NULL""",
            (data_key(),),
        )
        c.commit()


def load_ownership_network(gen: ScenarioGenerator) -> None:
    """Entities and edges from the registry (003_ownership_network.sql), plus rendezvous edges derived
    from the loaded positions with the same proximity rule the AIS detector uses."""
    ents, edges = build_graph(gen.registry())
    with connect() as c, c.cursor() as cur:
        ids: dict[str, int] = {}
        for e in ents.values():
            if e.real_name:
                cur.execute(
                    "SELECT entity_upsert(%s::text, %s::text, %s::text, %s::jsonb, pgp_sym_encrypt(%s::text, %s::text))",
                    (e.kind, e.key, e.name, Jsonb(e.attrs), e.real_name, data_key()),
                )
            else:
                cur.execute(
                    "SELECT entity_upsert(%s::text, %s::text, %s::text, %s::jsonb)",
                    (e.kind, e.key, e.name, Jsonb(e.attrs)),
                )
            ids[e.key] = cur.fetchone()[0]
        cur.execute(
            """SELECT a.mmsi AS mmsi_a, b.mmsi AS mmsi_b, min(a.ts) AS started_at, max(a.ts) AS ended_at, count(*) AS minutes
               FROM positions a JOIN positions b ON a.ts = b.ts AND a.mmsi < b.mmsi
               WHERE ST_DWithin(a.geom, b.geom, 926) AND coalesce(a.sog, 0) < 3 AND coalesce(b.sog, 0) < 3
               GROUP BY a.mmsi, b.mmsi HAVING count(*) >= 30"""
        )
        pairs = [
            dict(
                zip(
                    ["mmsi_a", "mmsi_b", "started_at", "ended_at", "minutes"],
                    r,
                    strict=True,
                )
            )
            for r in cur.fetchall()
        ]
        for p in pairs:
            for k in (f"vessel:{p['mmsi_a']}", f"vessel:{p['mmsi_b']}"):
                if k not in ids:
                    cur.execute(
                        "SELECT entity_upsert('vessel', %s::text, %s::text, %s::jsonb)",
                        (k, k.split(":")[1], Jsonb({"mmsi": int(k.split(":")[1])})),
                    )
                    ids[k] = cur.fetchone()[0]
        for e in edges + rendezvous_edges(pairs):
            cur.execute(
                "SELECT edge_upsert(%s::bigint, %s::bigint, %s::text, %s::text, %s::timestamptz, %s::timestamptz, %s::real, %s::jsonb)",
                (
                    ids[e.src],
                    ids[e.dst],
                    e.rel,
                    e.source,
                    e.since,
                    e.until,
                    e.confidence,
                    Jsonb(e.attrs),
                ),
            )
        c.commit()
    log.info(
        "ownership network: %d entities, %d edges (%d rendezvous)",
        len(ents),
        len(edges) + len(pairs),
        len(pairs),
    )


def bulk_load_positions(gen: ScenarioGenerator) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM positions WHERE source='synthetic'")
        # daily partitions for the scenario's range (ADR-0005)
        cur.execute(
            "SELECT positions_ensure_partitions(%s::date, %s::date)",
            (gen.positions[0].ts.date(), gen.positions[-1].ts.date()),
        )
        with cur.copy(
            "COPY positions (mmsi, ts, geom, sog, cog, nav_status, source) FROM STDIN"
        ) as cp:
            for p in gen.positions:
                cp.write_row(
                    (
                        p.mmsi,
                        p.ts,
                        f"SRID=4326;POINT({p.lon} {p.lat})",
                        p.sog,
                        p.cog,
                        p.nav_status,
                        "synthetic",
                    )
                )
        c.commit()
    log.info("bulk loaded %d synthetic positions", len(gen.positions))


#: How often the ingest task reports on itself, in seconds.
HEARTBEAT_S = int(os.getenv("FEED_HEARTBEAT_S", "60"))


async def heartbeat(health: FeedHealth, mode: str = "live") -> None:
    """Publish the feed heartbeat forever, connected or not (I4)."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        age, stored, dropped = health.take()
        await asyncio.to_thread(feed_metrics.publish, age, stored, dropped, mode)
        if age > HEARTBEAT_S * 5:
            log.warning("no position stored for %.0f s", age)


async def replay_stream(gen: ScenarioGenerator) -> None:
    """Stream the scenario at REPLAY_SPEED. With REPLAY_LOOP=true (default) it restarts after a
    short pause so a demo never goes quiet; the database already holds the full history."""
    r = redis.from_url(REDIS_URL)
    await r.delete(STREAM)
    # The same heartbeat as live mode, so one alarm covers both and a replay that
    # stalls is as visible as a silent feed (I4).
    health = FeedHealth()
    beat = asyncio.create_task(heartbeat(health, "replay"))
    beat.add_done_callback(lambda t: t.cancelled() or t.exception())
    while True:
        t_prev = gen.positions[0].ts
        log.info("replaying %d positions at %.0fx", len(gen.positions), SPEED)
        for p in gen.positions:
            wait = (p.ts - t_prev).total_seconds() / SPEED
            if wait > 0:
                await asyncio.sleep(wait)
            t_prev = p.ts
            await r.xadd(
                STREAM,
                {"data": json.dumps(p.as_dict())},
                maxlen=20000,
                approximate=True,
            )
            health.stored_one()
        await r.xadd(STREAM, {"data": json.dumps({"event": "replay_complete"})})
        log.info("replay complete")
        if not LOOP:
            return
        await asyncio.sleep(LOOP_PAUSE_S)


async def flush_batch(c, r, batch: list[dict], health) -> int:
    """Write one batch in a single transaction, then publish it. Returns rows stored.

    A row the validator let through can still be rejected by the database, so a failed
    batch is retried one row at a time: the batch is an efficiency, never a reason to
    lose a report we could have kept (P15, and P3's per-message guarantee).
    """
    if not batch:
        return 0
    vessels, positions = position_rows(batch)
    try:
        async with c.transaction():
            await c.cursor().executemany(
                "INSERT INTO vessels (mmsi, name, is_synthetic) VALUES (%s,%s,false) ON CONFLICT (mmsi) DO NOTHING",
                vessels,
            )
            await c.cursor().executemany(
                "INSERT INTO positions (mmsi, ts, geom, sog, cog, nav_status, source)"
                " VALUES (%s,%s,ST_GeogFromText(%s),%s,%s,%s,%s)",
                positions,
            )
    except psycopg.Error as e:
        log.warning("batch of %d failed (%s); retrying row by row", len(batch), e)
        kept = []
        for one in batch:
            v, pos = position_rows([one])
            try:
                async with c.transaction():
                    await c.cursor().executemany(
                        "INSERT INTO vessels (mmsi, name, is_synthetic) VALUES (%s,%s,false) ON CONFLICT (mmsi) DO NOTHING",
                        v,
                    )
                    await c.cursor().executemany(
                        "INSERT INTO positions (mmsi, ts, geom, sog, cog, nav_status, source)"
                        " VALUES (%s,%s,ST_GeogFromText(%s),%s,%s,%s,%s)",
                        pos,
                    )
                kept.append(one)
            except psycopg.Error as one_e:
                health.dropped_one()
                log.warning("dropped a report for %s (%s)", one["mmsi"], one_e)
        batch = kept
        if not batch:
            return 0
    pipe = r.pipeline()
    for one in batch:
        pipe.xadd(STREAM, {"data": json.dumps(one)}, maxlen=20000, approximate=True)
    await pipe.execute()
    for _ in batch:
        health.stored_one()
    return len(batch)


async def live_aisstream(areas: list[dict]) -> None:
    """Subscribe AISStream to every watched area in one subscription and write each
    position report as it arrives. Reconnects when the stream drops; nothing is injected
    in live mode."""
    import websockets

    key = os.environ.get("AISSTREAM_API_KEY", "")
    if not key:
        raise RuntimeError("AIS_MODE=live needs AISSTREAM_API_KEY")
    sub = {
        "APIKey": key,
        "BoundingBoxes": subscription_boxes(areas),
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }
    r = redis.from_url(REDIS_URL)
    partition_day = None
    received = 0
    dropped = 0
    health = FeedHealth()
    batch: list[dict] = []
    last_flush = time.monotonic()
    beat = asyncio.create_task(heartbeat(health, "live"))
    beat.add_done_callback(lambda t: t.cancelled() or t.exception())
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(
                DB_URL, autocommit=True
            ) as c:
                async with websockets.connect(
                    "wss://stream.aisstream.io/v0/stream"
                ) as ws:
                    await ws.send(json.dumps(sub))
                    log.info(
                        "AISStream connected for %d area(s): %s",
                        len(areas),
                        ", ".join(a["name"] for a in areas),
                    )
                    async for raw in ws:
                        today = datetime.now(UTC).date()
                        if partition_day != today:
                            await c.execute(
                                "SELECT positions_ensure_partitions(%s::date, %s::date + 1)",
                                (today, today),
                            )
                            partition_day = today
                        m = json.loads(raw)
                        if m.get("MessageType") == "ShipStaticData":
                            # Name, IMO, call sign, type and dimensions: the identity a real
                            # vessel carries in AIS, so the registry is not empty in live mode.
                            vessel, registry = static_rows(m)
                            if vessel:
                                await c.execute(*upsert_vessel_sql(vessel))
                                await c.execute(*upsert_registry_sql(registry))
                            continue
                        if m.get("MessageType") != "PositionReport":
                            if "error" in m:
                                raise RuntimeError(f"AISStream: {m['error']}")
                            # A quiet feed must still write what it has, or the last
                            # few reports wait for a batch that never fills.
                            if should_flush(len(batch), time.monotonic() - last_flush):
                                received += await flush_batch(c, r, batch, health)
                                batch, last_flush = [], time.monotonic()
                            continue
                        pr, meta = m["Message"]["PositionReport"], m["MetaData"]
                        # AIS sends 91/181 for "not available" and (0,0) as a
                        # default. PostGIS rejects the first (an uncaught DataError
                        # crash-looped this task) and the second manufactures false
                        # spoofs and rendezvous, so both are dropped here.
                        rec = position_record(pr, datetime.now(UTC).isoformat())
                        if rec is None:
                            dropped += 1
                            health.dropped_one()
                            if dropped % 500 == 0:
                                log.info(
                                    "AISStream: dropped %d unusable position reports",
                                    dropped,
                                )
                            continue
                        rec["name"] = (meta.get("ShipName") or "").strip()
                        batch.append(rec)
                        if should_flush(len(batch), time.monotonic() - last_flush):
                            before = received
                            received += await flush_batch(c, r, batch, health)
                            batch, last_flush = [], time.monotonic()
                            if received // 500 != before // 500:
                                log.info("AISStream: %d position reports", received)
        except (OSError, websockets.WebSocketException, RuntimeError) as e:
            log.warning("AISStream stream ended (%s); reconnecting in 10 s", e)
            await asyncio.sleep(10)


def store_meta(items: dict) -> None:
    """Publish scenario metadata for the API: the database is the shared channel on AWS,
    the /app/shared volume the local one (kept so a local API without the table still works)."""
    with connect() as c:
        for key, value in items.items():
            c.execute(
                """INSERT INTO scenario_meta (key, value, updated_at) VALUES (%s, %s::jsonb, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (key, json.dumps(value)),
            )
    os.makedirs("/app/shared", exist_ok=True)
    for key, value in items.items():
        with open(f"/app/shared/{key}.json", "w") as f:
            json.dump(value, f, indent=1)


def main() -> None:
    _wait_for_db()
    apply_schema()
    gen = ScenarioGenerator.from_file(SCENARIO).run()
    # The watched areas, for the UI (GET /area): the scenario's box always, plus the
    # catalogue areas WATCH_AREAS names. Live mode subscribes to all of them; replay data
    # exists only in the scenario's own box. `name`/`bbox` stay the scenario's for older
    # readers.
    area = {
        "name": gen.s.get("name", "scenario"),
        "bbox": gen.s["bbox"],
        "mode": MODE,
        "start_time": gen.s.get("start_time"),
        "duration_minutes": gen.s.get("duration_minutes"),
    }
    area["areas"] = select_areas(area, WATCH_AREAS, load_catalogue(AREAS_FILE))
    if MODE == "live":
        # The scenario's box is the only part of it that means anything against real
        # traffic. Everything else the generator produces is fiction, and it used to be
        # loaded onto real vessels: `DELETE FROM zones` replaced real geography with
        # invented anchorages, fabricated sanctions were written over real registry
        # rows, and /ground-truth served an answer key for anomalies that were never
        # injected. The empty ground truth is written explicitly, because a box
        # switched from replay to live would otherwise keep serving the old key (P13).
        store_meta({"ground_truth": [], "area": area})
        log.info("live mode: scenario reference data not loaded")
        asyncio.run(live_aisstream(area["areas"]))
        return
    load_reference(gen)
    store_meta({"ground_truth": gen.ground_truth(), "area": area})
    if PRELOAD:
        bulk_load_positions(gen)
    asyncio.run(replay_stream(gen))


if __name__ == "__main__":
    main()
