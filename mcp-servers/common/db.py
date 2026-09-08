import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

_pool: ConnectionPool | None = None


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


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            db_url(),
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def reopen_pool() -> None:
    """After a rotation: a fresh pool with the re-read password."""
    global _pool
    old, _pool = _pool, None
    if old is not None:
        try:
            old.close(timeout=1)
        except Exception:  # noqa: BLE001
            pass


class _Connection:
    """`pool().connection()` that survives a password rotation: on a pool timeout the
    secret is re-read and, if the password changed, the pool is rebuilt once."""

    def __enter__(self):
        try:
            self.cm = pool().connection()
            return self.cm.__enter__()
        except PoolTimeout:
            if not refresh_password():
                raise
            reopen_pool()
            self.cm = pool().connection()
            return self.cm.__enter__()

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


@contextmanager
def cursor():
    with _Connection() as conn, conn.cursor() as cur:
        yield cur


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return [dict(r) for r in cur.fetchall()]
