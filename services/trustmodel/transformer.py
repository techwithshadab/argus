"""Custom TrustModel telemetry transformer for Argus spans.

The wiki does not publish the exact envelope `agentic.evaluate(file_path=...)` expects, so the
plan is: emit the canonical shape first, and register this transformer if their loader rejects
it. Their docs put a new transformer at "~50 lines"; this is that fallback, kept ready so the
first paid evaluation is not spent discovering a format mismatch.

Import is lazy and guarded: the `trustmodel` package is not a hard dependency of this repo, and
nothing else in `services/trustmodel` should fail to import when it is absent.
"""

from __future__ import annotations

from typing import Any

VENDOR_NAME = "argus-otel"


def normalise(raw_span: dict[str, Any]) -> dict[str, Any]:
    """Argus/OTel span -> TrustModel canonical schema.

    Accepts both our own exported spans (already canonical) and raw OTel spans with
    `traceId`/`spanId`/`attributes`, so the same function works against a live OTLP feed.
    """
    attrs = raw_span.get("attributes") or {}
    meta = raw_span.get("metadata") or {}
    return {
        "trace_id": raw_span.get("trace_id") or raw_span.get("traceId"),
        "span_id": raw_span.get("span_id") or raw_span.get("spanId"),
        "ts": raw_span.get("ts")
        or raw_span.get("startTime")
        or raw_span.get("timestamp"),
        "model": raw_span.get("model")
        or attrs.get("gen_ai.request.model")
        or meta.get("model_id"),
        "input_hash": raw_span.get("input_hash", ""),
        "output_hash": raw_span.get("output_hash", ""),
        "tokens": raw_span.get("tokens")
        or {
            "input": attrs.get("gen_ai.usage.input_tokens"),
            "output": attrs.get("gen_ai.usage.output_tokens"),
        },
        "latency_ms": raw_span.get("latency_ms") or attrs.get("duration_ms"),
        "tool_calls": raw_span.get("tool_calls") or [],
        "metadata": {**attrs, **meta} or {},
    }


def register() -> bool:
    """Register with the installed SDK. Returns False when it is not installed."""
    try:
        from trustmodel.telemetry import (  # noqa: PLC0415
            BaseTelemetryTransformer,
            register_transformer,
        )
    except Exception:  # noqa: BLE001 - optional dependency, absence is not an error here
        return False

    class ArgusOtelTransformer(BaseTelemetryTransformer):  # type: ignore[misc, valid-type]
        vendor_name = VENDOR_NAME

        def transform(self, raw_span):  # noqa: D102
            return normalise(raw_span)

    register_transformer(ArgusOtelTransformer())
    return True
