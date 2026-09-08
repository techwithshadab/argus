"""Positions retention (ADR-0005): export every daily partition older than the hot window to
Parquet, verify the export, then drop the partition. Findings keep their own evidence snapshots,
so nothing a report cites depends on these rows.

Targets: ARCHIVE_BUCKET (S3, key positions/day=YYYY-MM-DD/<partition>.parquet) or ARCHIVE_DIR
(a directory, same layout). HOT_DAYS overrides retention_policy.positions. DRY_RUN=true exports
nothing and drops nothing; it only lists what would happen. Every drop is written to audit_events.

Runs as an ECS scheduled task on AWS (daily) and as `docker compose run --rm archiver` locally."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("archiver")

COLUMNS = ["mmsi", "ts", "lat", "lon", "sog", "cog", "heading", "nav_status", "source"]


def db_url() -> str:
    """DATABASE_URL if set, else assembled from PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD (ECS secrets)."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return f"postgresql://{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}@{os.environ['PGHOST']}:{os.getenv('PGPORT', '5432')}/{os.getenv('PGDATABASE', 'argus')}"


def archive_key(prefix: str, day: date, partition: str) -> str:
    return f"{prefix.strip('/')}/day={day.isoformat()}/{partition}.parquet"


def to_parquet(rows: list[tuple]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = {c: [r[i] for r in rows] for i, c in enumerate(COLUMNS)}
    table = pa.table(
        {
            "mmsi": pa.array(cols["mmsi"], pa.int64()),
            "ts": pa.array(cols["ts"], pa.timestamp("us", tz="UTC")),
            "lat": pa.array(cols["lat"], pa.float64()),
            "lon": pa.array(cols["lon"], pa.float64()),
            "sog": pa.array(cols["sog"], pa.float32()),
            "cog": pa.array(cols["cog"], pa.float32()),
            "heading": pa.array(cols["heading"], pa.float32()),
            "nav_status": pa.array(cols["nav_status"], pa.string()),
            "source": pa.array(cols["source"], pa.string()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


class Store:
    """S3 bucket or local directory; both verify the written object before a partition is dropped."""

    def __init__(self):
        self.bucket = os.getenv("ARCHIVE_BUCKET", "")
        self.directory = os.getenv("ARCHIVE_DIR", "")
        self.prefix = os.getenv("ARCHIVE_PREFIX", "positions")
        if not self.bucket and not self.directory:
            raise SystemExit("set ARCHIVE_BUCKET (S3) or ARCHIVE_DIR (directory)")
        self.s3 = None
        if self.bucket:
            import boto3

            self.s3 = boto3.client(
                "s3", region_name=os.getenv("AWS_REGION", "us-east-1")
            )

    def put(self, key: str, data: bytes) -> str:
        if self.s3:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ServerSideEncryption="aws:kms"
                if os.getenv("ARCHIVE_KMS_KEY_ID")
                else "AES256",
                **(
                    {"SSEKMSKeyId": os.environ["ARCHIVE_KMS_KEY_ID"]}
                    if os.getenv("ARCHIVE_KMS_KEY_ID")
                    else {}
                ),
            )
            size = self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
            if size != len(data):
                raise RuntimeError(
                    f"size mismatch after upload of {key}: {size} != {len(data)}"
                )
            return f"s3://{self.bucket}/{key}"
        path = os.path.join(self.directory, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        if os.path.getsize(path) != len(data):
            raise RuntimeError(f"size mismatch after write of {path}")
        return path


def main() -> int:
    import psycopg

    dry = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
    hot_days = int(os.environ["HOT_DAYS"]) if os.getenv("HOT_DAYS") else None
    store = None if dry else Store()
    archived = 0
    with psycopg.connect(db_url()) as conn:
        expired = conn.execute(
            "SELECT partition_name, day, row_estimate FROM positions_expired_partitions(%s)",
            (hot_days,),
        ).fetchall()
        if not expired:
            log.info(
                "nothing to archive (hot window %s days)",
                hot_days or "from retention_policy",
            )
            return 0
        for name, day, est in expired:
            rows = conn.execute(
                f"SELECT mmsi, ts, ST_Y(geom::geometry), ST_X(geom::geometry), sog, cog, heading, nav_status, source FROM {name} ORDER BY mmsi, ts"  # noqa: S608 (name validated by the SQL function)
            ).fetchall()
            key = archive_key(os.getenv("ARCHIVE_PREFIX", "positions"), day, name)
            if dry:
                log.info(
                    "would archive %s (%d rows, est %d) to %s",
                    name,
                    len(rows),
                    est,
                    key,
                )
                continue
            data = to_parquet(rows)
            location = store.put(key, data)
            conn.execute("SELECT positions_drop_partition(%s)", (name,))
            conn.execute(
                """INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details)
                   VALUES ('archiver', 'system', 'positions.archived', 'positions_partition', %s, %s)""",
                (
                    name,
                    json.dumps(
                        {
                            "day": day.isoformat(),
                            "rows": len(rows),
                            "bytes": len(data),
                            "location": location,
                        }
                    ),
                ),
            )
            conn.commit()
            archived += 1
            log.info(
                "archived %s: %d rows, %d bytes -> %s",
                name,
                len(rows),
                len(data),
                location,
            )
    log.info("done: %d partitions archived", archived)
    return 0


if __name__ == "__main__":
    sys.exit(main())
