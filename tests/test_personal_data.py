"""Personal data leaves the database only through the registry MCP tool (P2).

`registry.beneficial_owner` may name a real person, so it exists only as
pgcrypto ciphertext. The API and the UI stay pseudonymous: nothing the API
writes to `evidence_snapshots` may hold a decrypted owner, and nothing it
serves may hand one back. Before this was pinned, `/investigations` snapshotted
the registry row with `pgp_sym_decrypt` and `GET /evidence/{id}` returned that
payload with `SELECT *`, so a plaintext owner name was persisted in an
append-only table and served to every officer.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services/api/main.py").read_text()
REGISTRY_TOOL = (ROOT / "mcp-servers/servers/registry.py").read_text()


def test_only_the_registry_tool_decrypts_personal_data():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        skip = (".venv", "tests/", "infra/cdk/cdk.out/", "docs/", "evals/")
        if rel.startswith(skip) or path.name in ("datakey.py", "rekey.py"):
            continue
        if "pgp_sym_decrypt" in path.read_text():
            assert rel == "mcp-servers/servers/registry.py", rel
    assert "pgp_sym_decrypt(beneficial_owner_enc" in REGISTRY_TOOL


def test_api_never_decrypts_or_selects_the_owner_column():
    assert "pgp_sym_decrypt" not in API
    # The snapshot records presence, not the name.
    assert "(beneficial_owner_enc IS NOT NULL) AS beneficial_owner_on_file" in API
    assert "AS beneficial_owner," not in API


def test_evidence_endpoint_names_its_columns_and_redacts_the_payload():
    body = API.split('@app.get("/evidence/{snapshot_id}")')[1].split("\n@app.")[0]
    assert "SELECT *" not in body, (
        "an unnamed select re-exports whatever a writer added"
    )
    assert "redact_personal(" in body
    assert "Depends(current_officer)" in body


def test_redaction_covers_the_known_personal_keys_at_any_depth():
    ns: dict = {}
    src = API.split("#: Payload keys")[1].split("@app.get")[0]
    exec("PERSONAL_SNAPSHOT_KEYS" + src.split("PERSONAL_SNAPSHOT_KEYS", 1)[1], ns)  # noqa: S102
    redact = ns["redact_personal"]
    assert set(ns["PERSONAL_SNAPSHOT_KEYS"]) >= {"beneficial_owner"}
    payload = {
        "mmsi": 1,
        "beneficial_owner": "A Person",
        "nodes": [{"name": "Shell Co", "owner_name": "A Person"}],
    }
    out = redact(payload)
    assert out["beneficial_owner"] == "[redacted]"
    assert out["nodes"][0]["owner_name"] == "[redacted]"
    assert out["mmsi"] == 1 and out["nodes"][0]["name"] == "Shell Co"


def test_the_loader_clears_the_plaintext_column():
    replay = (ROOT / "services/ais-replay/replay.py").read_text()
    assert re.search(
        r"UPDATE registry SET beneficial_owner_enc = pgp_sym_encrypt\("
        r"beneficial_owner, %s\), beneficial_owner = NULL",
        replay,
    )
