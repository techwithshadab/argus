"""Password rotation (A6): the shared secret helpers are identical in every image and
re-read the secret only when it can change."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [
    "services/api/dbconn.py",
    "mcp-servers/common/db.py",
    "services/ais-replay/replay.py",
]


def shared_block(path: str) -> str:
    src = (ROOT / path).read_text()
    a = src.index("def _password() -> str:")
    b = src.index("\n\n\n", src.index("def db_url() -> str:"))
    return src[a:b]


def test_secret_helpers_identical_in_every_image():
    blocks = {p: shared_block(p) for p in COPIES}
    assert len(set(blocks.values())) == 1, "keep the three copies identical"


def _stub_psycopg(monkeypatch):
    """dbconn imports psycopg at module level; the unit env has no database driver."""
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    pool = types.ModuleType("psycopg_pool")
    pool.ConnectionPool = object
    pool.PoolTimeout = TimeoutError
    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool)


def _load(monkeypatch, secret: str):
    _stub_psycopg(monkeypatch)
    calls = []

    class Client:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": secret}

    def client(*a, **k):
        return Client()

    fake = types.ModuleType("boto3")
    fake.client = client
    monkeypatch.setitem(sys.modules, "boto3", fake)
    spec = importlib.util.spec_from_file_location(
        "dbconn", ROOT / "services/api/dbconn.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def test_refresh_only_when_a_secret_arn_exists(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "old")
    monkeypatch.delenv("PGPASSWORD_SECRET_ARN", raising=False)
    mod, calls = _load(monkeypatch, '{"password": "new"}')
    assert mod.refresh_password() is False and not calls


def test_refresh_reports_a_rotation(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "old")
    monkeypatch.setenv("PGPASSWORD_SECRET_ARN", "arn:secret")
    for k, v in {"PGUSER": "argus", "PGHOST": "db"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    mod, calls = _load(monkeypatch, '{"password": "new"}')
    assert mod.refresh_password() is True
    assert calls == ["arn:secret"]
    assert mod.db_url() == "postgresql://argus:new@db:5432/argus"
    # Same password again: not a rotation.
    assert mod.refresh_password() is False


def test_refresh_keeps_the_old_password_when_the_read_fails(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "old")
    monkeypatch.setenv("PGPASSWORD_SECRET_ARN", "arn:secret")

    class Boom(types.ModuleType):
        def client(self, *a, **k):
            raise RuntimeError("no network")

    _stub_psycopg(monkeypatch)
    monkeypatch.setitem(sys.modules, "boto3", Boom("boto3"))
    spec = importlib.util.spec_from_file_location(
        "dbconn", ROOT / "services/api/dbconn.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(RuntimeError):
        mod.refresh_password()
    assert mod._password() == "old"


def _replay_connect():
    """replay.py's `connect` alone: the module needs a database and Redis to import."""
    src = (ROOT / "services/ais-replay/replay.py").read_text()
    a = src.index("def connect(**kw):")
    return src[a : src.index("\n\n\n", a)]


def test_replay_connect_reconnects_once_after_a_rotation():
    calls = []

    class Failed(Exception):
        pass

    class Psycopg:
        OperationalError = Failed

        @staticmethod
        def connect(url, **kw):
            calls.append((url, kw))
            if len(calls) == 1:
                raise Failed("password authentication failed")
            return "conn"

    ns = {
        "psycopg": Psycopg,
        "DB_URL": "old",
        "refresh_password": lambda: True,
        "db_url": lambda: "new",
    }
    exec(_replay_connect(), ns)
    assert ns["connect"](autocommit=True) == "conn"
    assert calls == [("old", {"autocommit": True}), ("new", {"autocommit": True})]


def test_replay_connect_raises_other_errors_unchanged():
    class Failed(Exception):
        pass

    class Psycopg:
        OperationalError = Failed

        @staticmethod
        def connect(url, **kw):
            raise Failed("connection refused")

    ns = {"psycopg": Psycopg, "DB_URL": "u", "refresh_password": lambda: True}
    exec(_replay_connect(), ns)
    with pytest.raises(Failed, match="refused"):
        ns["connect"]()
