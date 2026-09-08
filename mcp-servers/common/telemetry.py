"""OpenTelemetry bootstrap for MCP servers. Exports to the collector when
OTEL_EXPORTER_OTLP_ENDPOINT is set, otherwise stays inert (no-op tracer)."""

import functools
import json
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure(service_name: str) -> None:
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "argus",
            "deployment.environment": os.getenv("DEPLOY_ENV", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
            )
        )
    trace.set_tracer_provider(provider)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        HTTPXClientInstrumentor().instrument()
        PsycopgInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass


def _server_version() -> str:
    try:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1].joinpath("VERSION").read_text().strip()
        )
    except OSError:
        return "unversioned"


SERVER_VERSION = _server_version()


def traced_tool(fn):
    """Wrap an MCP tool so every invocation is a span carrying its arguments and result size."""
    tracer = trace.get_tracer("argus.mcp")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with tracer.start_as_current_span(f"mcp.tool {fn.__name__}") as span:
            span.set_attribute("mcp.tool.name", fn.__name__)
            span.set_attribute("mcp.server.version", SERVER_VERSION)
            span.set_attribute("mcp.tool.args", json.dumps(kwargs, default=str)[:2000])
            result = fn(*args, **kwargs)
            span.set_attribute(
                "mcp.tool.result_chars", len(json.dumps(result, default=str))
            )
            return result

    return wrapper
