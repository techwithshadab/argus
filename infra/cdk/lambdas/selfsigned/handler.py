"""Custom resource: a self-signed TLS certificate imported into ACM.

The public ALB needs an HTTPS listener before it can sign officers in (ALB authentication
actions exist only on HTTPS listeners) and no trusted certificate can be issued for an
`*.elb.amazonaws.com` name. Deployers with a domain pass `-c uiCertificateArn`; everyone else
gets this certificate, generated here at deploy time so the private key never enters a
template, a log or a repository. Browsers warn about it once; the session is encrypted.

Create: generate an RSA key and a certificate for the load balancer's DNS name, import them
into ACM, return the certificate ARN as the physical id. Update with the same name: keep the
certificate. Delete: remove it from ACM once the listener that used it is gone."""

from __future__ import annotations

import datetime as dt
import logging

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

VALID_DAYS = 825


def self_signed(dns_name: str, days: int = VALID_DAYS) -> tuple[bytes, bytes]:
    """PEM (certificate, private key) for one DNS name."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name[:64])])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


def data(arn: str, cert: bytes, dns_name: str = "") -> dict:
    """Attributes: the ARN for listeners, the public certificate for clients' trust, and
    the name in lowercase (Cognito requires lowercase callback URLs; a load balancer's
    generated DNS name is mixed case and CloudFormation cannot lowercase a string)."""
    return {
        "CertificateArn": arn,
        "CertificatePem": cert.decode(),
        "DnsNameLower": dns_name.lower(),
    }


def on_event(event, _context):
    props = event.get("ResourceProperties") or {}
    dns_name = props["DnsName"].lower()
    acm = boto3.client("acm")
    kind = event["RequestType"]
    if kind == "Create":
        cert, key = self_signed(dns_name)
        arn = acm.import_certificate(
            Certificate=cert,
            PrivateKey=key,
            Tags=[{"Key": "project", "Value": "argus"}],
        )["CertificateArn"]
        log.info("imported self-signed certificate %s for %s", arn, dns_name)
        return {"PhysicalResourceId": arn, "Data": data(arn, cert, dns_name)}
    arn = event["PhysicalResourceId"]
    if kind == "Update":
        old = ((event.get("OldResourceProperties") or {}).get("DnsName") or "").lower()
        if old == dns_name:
            pem = acm.get_certificate(CertificateArn=arn)["Certificate"]
            return {
                "PhysicalResourceId": arn,
                "Data": data(arn, pem.encode(), dns_name),
            }
        cert, key = self_signed(dns_name)
        new = acm.import_certificate(Certificate=cert, PrivateKey=key)["CertificateArn"]
        return {"PhysicalResourceId": new, "Data": data(new, cert, dns_name)}
    try:
        acm.delete_certificate(CertificateArn=arn)
    except acm.exceptions.ResourceNotFoundException:
        pass
    except acm.exceptions.ResourceInUseException:
        # The listener is deleted in the same stack operation; leave the certificate for
        # the next run rather than fail the rollback (a `cdk gc`-style cleanup is manual).
        log.warning("certificate %s still in use; not deleted", arn)
    return {"PhysicalResourceId": arn}
