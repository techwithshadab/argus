"""Trust for the platform's internal load balancer (A7).

The internal ALB terminates TLS for the API with a certificate the deploy generates for
the balancer's own name; its public half is the SSM parameter named by INTERNAL_CA_SSM.
Agents fetch it once and hand the file to httpx as the CA bundle. Without the parameter
(local compose, plain http) the system trust store applies."""

from __future__ import annotations

import os
import tempfile

from .config import settings

_path: str | None = None


def ca_bundle() -> str | bool:
    """httpx `verify`: the path of the platform CA file, or True for the system store."""
    global _path
    name = settings.internal_ca_ssm
    if not name:
        return True
    if _path:
        return _path
    import boto3

    pem = boto3.client("ssm", region_name=settings.aws_region).get_parameter(Name=name)[
        "Parameter"
    ]["Value"]
    path = os.path.join(tempfile.gettempdir(), "argus-internal-ca.pem")
    with open(path, "w") as f:
        f.write(pem)
    _path = path
    return path
