"""Deployment data key for column-level encryption of personal data (pgcrypto pgp_sym_*).

Locally the key is DATA_KEY. On AWS it is DATA_KEY_SECRET_ARN, a Secrets Manager secret encrypted
with the Argus KMS key, read once and cached. After a re-key (services/api/rekey.py rewrites every
encrypted column under a new key, then stores it) a cached key stops decrypting: `decrypting`
re-reads the secret once on pgcrypto's "Wrong key" error and retries. Duplicated verbatim in
mcp-servers/common, services/api and services/ais-replay so each image stays self-contained;
change all three together (a unit test pins them identical).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


@functools.lru_cache(maxsize=1)
def data_key() -> str:
    key = os.getenv("DATA_KEY")
    if key:
        return key
    arn = os.getenv("DATA_KEY_SECRET_ARN")
    if arn:
        import boto3

        client = boto3.client(
            "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        return client.get_secret_value(SecretId=arn)["SecretString"].strip()
    raise RuntimeError(
        "personal-data encryption needs DATA_KEY (local) or DATA_KEY_SECRET_ARN (AWS)"
    )


def wrong_key(exc: BaseException) -> bool:
    """pgcrypto's error when a ciphertext was made under another key."""
    return "wrong key" in str(exc).lower() or "corrupt data" in str(exc).lower()


def decrypting[T](run: Callable[[str], T]) -> T:
    """Call `run(key)`; on a wrong-key error re-read the secret once and call it again."""
    try:
        return run(data_key())
    except Exception as e:  # noqa: BLE001
        if not wrong_key(e) or not os.getenv("DATA_KEY_SECRET_ARN"):
            raise
        data_key.cache_clear()
        return run(data_key())
