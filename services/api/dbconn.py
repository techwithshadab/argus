"""Database connection for the API and the job worker.

The password comes from ECS (PGPASSWORD, injected at task start) or from the Secrets
Manager secret named by PGPASSWORD_SECRET_ARN. The secret is rotated every 30 days
(data_stack.py), and a task that outlives a rotation keeps its old password: the first
connection attempt after the rotation fails, the secret is re-read, and the pool is
rebuilt with the new password (RotatingPool). Nothing restarts, nothing is lost.

`_password`, `refresh_password` and `db_url` are duplicated verbatim in
mcp-servers/common/db.py and services/ais-replay/replay.py so each image stays
self-contained; a unit test keeps the three copies identical."""

from __future__ import annotations

import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

log = logging.getLogger("dbconn")


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


class RotatingPool:
    """A psycopg connection pool that survives a password rotation: when the pool cannot
    hand out a connection, the secret is re-read and, if the password changed, the pool
    is rebuilt. Use `with pool.connection() as conn:` exactly like a ConnectionPool."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._pool = self._open()

    def _open(self) -> ConnectionPool:
        return ConnectionPool(
            db_url(), kwargs={"row_factory": dict_row}, open=True, **self._kwargs
        )

    def reopen(self) -> None:
        old, self._pool = self._pool, self._open()
        try:
            old.close(timeout=1)
        except Exception:  # noqa: BLE001
            pass

    def connection(self) -> _Connection:
        return _Connection(self)

    def close(self) -> None:
        self._pool.close()


class _Connection:
    def __init__(self, owner: RotatingPool):
        self.owner = owner
        self.cm = None

    def __enter__(self):
        try:
            self.cm = self.owner._pool.connection()
            return self.cm.__enter__()
        except PoolTimeout as e:
            # The pool retries in the background and times out; if the secret changed
            # meanwhile the old password is the reason, and the pool must be rebuilt.
            if not refresh_password():
                raise
            log.warning("database password rotated; reopening the pool (%s)", e)
            self.owner.reopen()
            self.cm = self.owner._pool.connection()
            return self.cm.__enter__()

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)
