"""Deploy-time policy shared by the stacks: pause mode and the model allowlist.

Lifecycle verbs (all through CDK, see the Makefile):
  deploy / update   cdk deploy --all
  stop              cdk deploy --all -c paused=true   (compute to zero, Aurora auto-pauses)
  start             cdk deploy --all -c paused=false
  delete            cdk destroy --all, then cdk gc for bootstrap assets
"""

from __future__ import annotations

from constructs import Construct

# Bedrock model vendors served on-demand by Bedrock itself. Anything else (Bedrock Marketplace
# models on SageMaker endpoints, imported models) carries hosting cost and is refused.
DEFAULT_MODEL_VENDORS = (
    "amazon",
)  # Nova only; widen with -c bedrockModelVendors=amazon,meta
# Cross-region inference profile prefixes that may precede the vendor.
_PROFILE_PREFIXES = ("us.", "eu", "apac.", "global.", "jp.", "au.", "ca.", "us-gov.")


def _flag(scope: Construct, key: str, default: bool = False) -> bool:
    raw = scope.node.try_get_context(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def is_paused(scope: Construct) -> bool:
    """`-c paused=true`: every ECS service to desired count 0 and Aurora allowed to auto-pause."""
    return _flag(scope, "paused")


def allowed_model_vendors(scope: Construct) -> tuple[str, ...]:
    raw = scope.node.try_get_context("bedrockModelVendors")
    if not raw:
        return DEFAULT_MODEL_VENDORS
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(v.strip().lower() for v in raw if v.strip())


def model_vendor(model_id: str) -> str:
    """Vendor namespace of a Bedrock model id or inference profile id.

    'us.anthropic.claude-sonnet-4-6' -> 'anthropic'; 'amazon.nova-pro-v1:0' -> 'amazon'."""
    mid = model_id.strip().lower()
    if mid.startswith("arn:"):
        # Only foundation-model / inference-profile ARNs are Bedrock-native.
        parts = mid.split(":", 5)
        resource = parts[5] if len(parts) > 5 else ""
        kind, _, name = resource.partition("/")
        if kind not in ("foundation-model", "inference-profile"):
            return ""
        mid = name
    for p in _PROFILE_PREFIXES:
        if mid.startswith(p):
            mid = mid[len(p) :].lstrip(".")
            break
    return mid.split(".", 1)[0] if "." in mid else ""


def validate_model_choice(scope: Construct, provider: str, model_id: str) -> None:
    """Refuse anything but Bedrock in AWS deployments (ADR-0002) and anything but the allowed
    Bedrock vendors (no Marketplace or imported models)."""
    if provider != "bedrock" and not _flag(scope, "allowExternalModelProviders"):
        raise ValueError(
            f"modelProvider={provider!r} is not allowed in AWS deployments (ADR-0002). "
            "Production is Bedrock-only; pass -c allowExternalModelProviders=true only for a "
            "non-production evaluation stack."
        )
    if provider == "bedrock":
        vendors = allowed_model_vendors(scope)
        vendor = model_vendor(model_id)
        if vendor not in vendors:
            raise ValueError(
                f"modelId={model_id!r} is not a Bedrock on-demand model from {vendors}. "
                "Bedrock Marketplace and imported models are refused to avoid endpoint hosting "
                "costs; override with -c bedrockModelVendors=anthropic,amazon,<vendor> if needed."
            )
