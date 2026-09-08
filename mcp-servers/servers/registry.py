"""Vessel registry MCP server.

Backed by the deployment's registry table. When OPENSANCTIONS_API_KEY is set, sanctions_screen
also queries the OpenSanctions API (https://api.opensanctions.org) so the same tool works
against real entity data. Results always say which source they came from."""

from __future__ import annotations

import json
import logging
import os

import httpx
from common.datakey import decrypting
from common.db import q
from common.identity import workload_access_token
from common.safety import untrusted
from common.telemetry import traced_tool
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

mcp = FastMCP(
    "registry",
    instructions="Vessel identity, ownership, flag history, fleet associations and sanctions screening.",
    stateless_http=True,
    json_response=True,
)

OPENSANCTIONS = "https://api.opensanctions.org"


def _identity_api_key() -> str | None:
    """The OpenSanctions key from AgentCore Identity's token vault: the runtime injects
    this invocation's workload access token, which is exchanged for the API key held by
    the credential provider named in `OPENSANCTIONS_CREDENTIAL_PROVIDER`. Cached for the
    process; None (fall back to the plain secret) when Identity is not configured."""
    provider = os.getenv("OPENSANCTIONS_CREDENTIAL_PROVIDER")
    token = workload_access_token()
    if not provider or not token:
        return None
    cached = os.environ.get("OPENSANCTIONS_API_KEY")
    if cached:
        return cached
    import boto3

    dp = boto3.client(
        "bedrock-agentcore", region_name=os.getenv("AWS_REGION", "us-east-1")
    )
    try:
        key = dp.get_resource_api_key(
            workloadIdentityToken=token, resourceCredentialProviderName=provider
        )["apiKey"]
    except Exception as e:  # noqa: BLE001
        log.warning("identity token vault lookup failed, using the secret: %s", e)
        return None
    os.environ["OPENSANCTIONS_API_KEY"] = key
    log.info("OpenSanctions key obtained from AgentCore Identity (%s)", provider)
    return key


def _secret_env(name: str) -> str | None:
    """`<NAME>_SECRET_ARN` -> the secret's value, cached in the environment. AgentCore
    Runtime cannot inject secrets the way ECS does, so runtimes carry the ARN instead."""
    arn = os.getenv(f"{name}_SECRET_ARN")
    if not arn:
        return None
    import boto3

    value = boto3.client(
        "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
    ).get_secret_value(SecretId=arn)["SecretString"]
    if value.startswith("{"):
        # the Identity credential provider references the same secret by JSON key
        value = json.loads(value).get("api_key", "")
    os.environ[name] = value
    return value


@mcp.tool()
@traced_tool
def lookup_vessel(mmsi: int | None = None, imo: int | None = None) -> dict:
    """Identity and ownership record for a vessel by MMSI or IMO. Includes flag history and notes."""
    rows = decrypting(
        lambda k: q(
            """SELECT mmsi, imo, name, flag, flag_history, registered_owner, operator,
                  pgp_sym_decrypt(beneficial_owner_enc, %s) AS beneficial_owner, sanctions, fleet, notes
           FROM registry WHERE (%s::bigint IS NOT NULL AND mmsi=%s) OR (%s::bigint IS NOT NULL AND imo=%s)""",
            (k, mmsi, mmsi, imo, imo),
        )
    )
    if rows:
        row = dict(rows[0])
        row["notes"] = untrusted(row.get("notes"), "registry notes")
        row["name"] = untrusted(row.get("name"), "ais static")
        return {"source": "local-registry", **row}
    static = q(
        "SELECT mmsi, imo, name, flag, ship_type, length_m FROM vessels WHERE (%s::bigint IS NOT NULL AND mmsi=%s) OR (%s::bigint IS NOT NULL AND imo=%s)",
        (mmsi, mmsi, imo, imo),
    )
    if static:
        static[0] = {
            **static[0],
            "name": untrusted(static[0].get("name"), "ais static"),
        }
        return {
            "source": "ais-static-only",
            **static[0],
            "note": "No registry record. Only AIS static data is available.",
        }
    return {"error": "vessel not found", "mmsi": mmsi, "imo": imo}


@mcp.tool()
@traced_tool
def sanctions_screen(entity_name: str) -> dict:
    """Screen a vessel, owner or operator name against sanctions lists.
    Uses the local list, and OpenSanctions when an API key is configured."""
    hits = []
    rows = decrypting(
        lambda k: q(
            "SELECT mmsi, name, registered_owner, operator, pgp_sym_decrypt(beneficial_owner_enc, %s) AS beneficial_owner, sanctions FROM registry",
            (k,),
        )
    )
    needle = entity_name.lower()
    for r in rows:
        for entry in r["sanctions"] or []:
            names = [
                r["name"],
                r["registered_owner"],
                r["operator"],
                r["beneficial_owner"],
                entry.get("entry", ""),
            ]
            if any(n and needle in n.lower() for n in names):
                hits.append(
                    {
                        "source": "local-list",
                        "matched_vessel_mmsi": r["mmsi"],
                        **entry,
                    }
                )
    key = (
        os.getenv("OPENSANCTIONS_API_KEY")
        or _identity_api_key()
        or _secret_env("OPENSANCTIONS_API_KEY")
    )
    if key:
        try:
            resp = httpx.post(
                f"{OPENSANCTIONS}/match/default",
                params={"algorithm": "best"},
                headers={"Authorization": f"ApiKey {key}"},
                json={
                    "queries": {
                        "q": {
                            "schema": "LegalEntity",
                            "properties": {"name": [entity_name]},
                        }
                    }
                },
                timeout=15,
            )
            for m in (
                resp.json().get("responses", {}).get("q", {}).get("results", [])[:5]
            ):
                hits.append(
                    {
                        "source": "opensanctions",
                        "id": untrusted(m["id"], "opensanctions"),
                        "caption": untrusted(m["caption"], "opensanctions"),
                        "score": m["score"],
                        "datasets": untrusted(
                            ", ".join(m.get("datasets", [])), "opensanctions"
                        ),
                    }
                )
        except Exception as e:  # noqa: BLE001
            hits.append({"source": "opensanctions", "error": str(e)})
    return {
        "query": entity_name,
        "hits": hits,
        "screened_sources": ["local-list"] + (["opensanctions"] if key else []),
    }


@mcp.tool()
@traced_tool
def fleet_associations(mmsi: int) -> dict:
    """Other vessels under the same registered owner, operator or beneficial owner."""
    rows = decrypting(
        lambda k: q(
            "SELECT registered_owner, operator, pgp_sym_decrypt(beneficial_owner_enc, %s) AS beneficial_owner, fleet FROM registry WHERE mmsi=%s",
            (k, mmsi),
        )
    )
    if not rows:
        return {"mmsi": mmsi, "associations": []}
    r = rows[0]
    siblings = decrypting(
        lambda k: q(
            """SELECT mmsi, name, flag FROM registry
           WHERE mmsi<>%s AND (registered_owner=%s OR operator=%s OR pgp_sym_decrypt(beneficial_owner_enc, %s)=%s)""",
            (mmsi, r["registered_owner"], r["operator"], k, r["beneficial_owner"]),
        )
    )
    return {
        "mmsi": mmsi,
        "declared_fleet": r["fleet"],
        "same_ownership_in_registry": siblings,
    }


@mcp.tool()
@traced_tool
def flag_history(mmsi: int) -> dict:
    """Flag changes over time. Frequent reflagging is a classic deceptive-shipping indicator."""
    rows = q("SELECT flag, flag_history FROM registry WHERE mmsi=%s", (mmsi,))
    if not rows:
        return {"mmsi": mmsi, "flag_history": []}
    hist = rows[0]["flag_history"] or []
    return {
        "mmsi": mmsi,
        "current_flag": rows[0]["flag"],
        "flag_history": hist,
        "changes": max(len(hist) - 1, 0),
    }


@mcp.tool()
@traced_tool
def ownership_network(mmsi: int, depth: int = 2) -> dict:
    """The ownership network around a vessel, up to `depth` hops: registered owner, operator and
    beneficial owner, the other vessels they own or operate, sanction listings of any party, flag
    history, and vessels this one has rendezvoused with at sea. Returns nodes, typed edges, and the
    shortest path from the vessel to every sanction listing reached. depth=2 answers "who controls
    this vessel and are they listed"; depth=3 shows what the controllers' other vessels connect to."""
    depth = max(1, min(int(depth), 4))
    net = q("SELECT ownership_network(%s, %s) AS net", (f"vessel:{mmsi}", depth))[0][
        "net"
    ]
    if not net.get("found"):
        return {
            "mmsi": mmsi,
            "found": False,
            "note": "vessel is not in the ownership network",
        }
    persons = [n for n in net["nodes"] if n["kind"] == "person"]
    if persons:
        dec = decrypting(
            lambda k: q(
                "SELECT id, pgp_sym_decrypt(name_enc, %s) AS name FROM entities WHERE id = ANY(%s) AND name_enc IS NOT NULL",
                (k, [n["id"] for n in persons]),
            )
        )
        names = {r["id"]: r["name"] for r in dec}
        for n in persons:
            n["name"] = names.get(n["id"], n["name"])
            n["personal_data"] = True
    by_id = {n["id"]: n for n in net["nodes"]}
    for e in net["edges"]:
        e["src_name"], e["dst_name"] = by_id[e["src"]]["name"], by_id[e["dst"]]["name"]
    kinds: dict[str, int] = {}
    for n in net["nodes"]:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    net["summary"] = {
        "mmsi": mmsi,
        "nodes_by_kind": kinds,
        "sanction_listings_reached": len(net["listed"]),
        "rendezvous_partners": [
            by_id[e["dst"] if by_id[e["src"]]["key"] == f"vessel:{mmsi}" else e["src"]][
                "name"
            ]
            for e in net["edges"]
            if e["rel"] == "rendezvoused_with"
            and f"vessel:{mmsi}" in (by_id[e["src"]]["key"], by_id[e["dst"]]["key"])
        ],
    }
    return net
