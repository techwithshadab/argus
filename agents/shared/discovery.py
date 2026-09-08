"""Agent discovery through AWS Agent Registry (ADR-0013).

The orchestrator and the worker find the specialist agents by asking the registry for the
approved record `argus-agent-<name>` and reading the URL from its A2A card, instead of
carrying endpoint URLs in their environment. The environment stays as the fallback so a
registry outage never stops an investigation; which path was used is logged."""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("discovery")
_cache: dict[str, tuple[str, float]] = {}
TTL_S = 300


def _registry_id_param() -> str | None:
    """SSM `/argus/registry-id` (written by the agents stack) when the environment does not
    name the registry; None locally."""
    if not os.getenv("AWS_REGION") or os.getenv("ARGUS_NO_REGISTRY"):
        return None
    try:
        import boto3

        return boto3.client("ssm", region_name=os.getenv("AWS_REGION")).get_parameter(
            Name="/argus/registry-id"
        )["Parameter"]["Value"]
    except Exception:  # noqa: BLE001
        return None


def card_url(client, registry_id: str, record_name: str) -> str | None:
    """The A2A card URL of the APPROVED record `record_name`. Listing returns names and
    status only; the descriptors (the card) come from BatchGetDiscoverableRegistryRecord."""
    token = None
    while True:
        kwargs = {"registryId": registry_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = client.list_discoverable_registry_records(**kwargs)
        for rec in page.get("registryRecords", []):
            if rec.get("name") == record_name and rec.get("status") == "APPROVED":
                got = client.batch_get_discoverable_registry_record(
                    entries=[
                        {"registryId": registry_id, "recordIds": [rec["recordId"]]}
                    ]
                )
                for full in got.get("registryRecords", []):
                    card = full.get("descriptors", {}).get("a2aAgentCard", {})
                    if card.get("data"):
                        return json.loads(card["data"]).get("url")
                return None
        token = page.get("nextToken")
        if not token:
            return None


def registry_url(
    name: str, registry_id: str | None = None, region: str | None = None
) -> str | None:
    """The A2A URL of the approved record `argus-agent-<name>`, or None."""
    registry_id = registry_id or os.getenv("ARGUS_REGISTRY_ID") or _registry_id_param()
    if not registry_id:
        return None
    hit = _cache.get(name)
    if hit and time.time() - hit[1] < TTL_S:
        return hit[0]
    try:
        import boto3

        c = boto3.client(
            "agent-registry", region_name=region or os.getenv("AWS_REGION", "us-east-1")
        )
        url = card_url(c, registry_id, f"argus-agent-{name}")
        if url:
            _cache[name] = (url, time.time())
            log.info("agent %s resolved from the registry: %s", name, url)
            return url
    except Exception as e:  # noqa: BLE001
        log.warning("registry lookup for %s failed: %s", name, e)
    return None


def agent_url(name: str, fallback: str) -> str:
    """Registry first, environment second."""
    return registry_url(name) or fallback
