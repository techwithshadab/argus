"""Thin client for the platform API (alerts, investigations, tasking). Carries the agent's
verified caller identity when TOOL_AUTH=aws-iam."""

from __future__ import annotations

import httpx

from .caller_auth import tool_auth
from .config import settings
from .tls import ca_bundle


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.api_url, timeout=30, auth=tool_auth(), verify=ca_bundle()
    )


def post(path: str, payload: dict) -> dict:
    with _client() as c:
        r = c.post(path, json=payload)
        r.raise_for_status()
        return r.json()


def get(path: str) -> dict:
    with _client() as c:
        r = c.get(path)
        r.raise_for_status()
        return r.json()
