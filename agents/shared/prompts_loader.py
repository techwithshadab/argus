"""Prompts: the repository files are the source of truth; on AWS the deploy publishes each
one to Bedrock Prompt Management and the runtime reads that managed version (ADR-0012),
so the provenance manifest carries a version number an auditor can open in the console,
next to the content hash that ties it to git."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
log = logging.getLogger("prompts")
_managed: dict[str, tuple[str, str]] = {}  # name -> (text, version)


def _managed_prompt(name: str) -> tuple[str, str] | None:
    """(text, version) from Bedrock Prompt Management when PROMPT_ARN_<NAME> is set."""
    arn = os.getenv(f"PROMPT_ARN_{name.upper()}")
    if not arn:
        return None
    if name in _managed:
        return _managed[name]
    version = os.getenv(f"PROMPT_VERSION_{name.upper()}") or "DRAFT"
    try:
        import boto3

        kwargs = {"promptIdentifier": arn}
        if version != "DRAFT":
            kwargs["promptVersion"] = version
        resp = boto3.client(
            "bedrock-agent", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).get_prompt(**kwargs)
        variant = next(
            v
            for v in resp["variants"]
            if v["name"] == resp.get("defaultVariant", "default")
        )
        text = variant["templateConfiguration"]["text"]["text"]
        _managed[name] = (text, str(resp.get("version", version)))
        log.info("prompt %s from Bedrock Prompt Management, version %s", name, version)
    except Exception as e:  # noqa: BLE001
        # The file is the same text the deploy published; availability wins, and the
        # manifest says "file" so the fallback is visible.
        log.warning(
            "managed prompt %s unavailable (%s); using the repository file", name, e
        )
        _managed[name] = ((PROMPTS / f"{name}.md").read_text(), "file")
    return _managed[name]


def load_prompt(name: str, **kwargs) -> str:
    managed = _managed_prompt(name)
    text = managed[0] if managed else (PROMPTS / f"{name}.md").read_text()
    return text.format(**kwargs) if kwargs else text


def prompt_hash(name: str) -> str:
    """Short content hash of the prompt in use, recorded in the manifest (ADR-0006)."""
    managed = _managed_prompt(name)
    data = managed[0].encode() if managed else (PROMPTS / f"{name}.md").read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]


def prompt_version(name: str) -> str:
    """The managed version number on AWS, "file" locally or when the fetch failed."""
    managed = _managed_prompt(name)
    return managed[1] if managed else "file"
