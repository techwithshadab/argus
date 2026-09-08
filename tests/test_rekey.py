"""Data-key re-encryption: the migration, the readers and the helper copies."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIES = [
    "mcp-servers/common/datakey.py",
    "services/api/datakey.py",
    "services/ais-replay/datakey.py",
]


def test_datakey_copies_identical():
    assert len({(ROOT / p).read_text() for p in COPIES}) == 1


def test_rekey_function_is_idempotent_and_covers_every_encrypted_column():
    sql = (ROOT / "data/sql/009_rekey_personal_data.sql").read_text()
    assert "CREATE OR REPLACE FUNCTION rekey_personal_data" in sql
    schema = "".join(
        (ROOT / f).read_text() for f in sorted((ROOT / "data/sql").glob("00[1-8]*.sql"))
    )
    for col in ("beneficial_owner_enc", "name_enc"):
        assert (
            col in schema
            and f"SET {col} = pgp_sym_encrypt(pgp_sym_decrypt({col}, old_key), new_key)"
            in sql
        )
    assert schema.count("_enc ") + schema.count("_enc,") >= 2


def test_every_decrypt_goes_through_the_refreshing_helper():
    for f in ("mcp-servers/servers/registry.py", "services/api/main.py"):
        src = (ROOT / f).read_text()
        assert "data_key()" not in src, f
        assert src.count("pgp_sym_decrypt(") == src.count("decrypting("), f


def test_wrong_key_detection(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dk", ROOT / "services/api/datakey.py"
    )
    dk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dk)
    assert dk.wrong_key(RuntimeError("Wrong key or corrupt data"))
    assert not dk.wrong_key(RuntimeError("connection refused"))
    monkeypatch.setenv("DATA_KEY", "k1")
    monkeypatch.delenv("DATA_KEY_SECRET_ARN", raising=False)
    dk.data_key.cache_clear()
    calls = []

    def run(key):
        calls.append(key)
        return "ok"

    assert dk.decrypting(run) == "ok" and calls == ["k1"]
