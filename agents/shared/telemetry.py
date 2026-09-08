"""One OpenTelemetry pipeline for every agent, regardless of framework.

Strands emits GenAI semantic-convention spans natively; LangGraph is instrumented with
OpenInference. Locally both export OTLP to the collector (`OTEL_EXPORTER_OTLP_ENDPOINT`),
which fans out to Tempo, Loki, Prometheus and the trace store. On AgentCore Runtime the ADOT
distro owns the provider and ships unified telemetry to the agent's CloudWatch log group
(what AgentCore Observability, Evaluations and Policy read); `ARGUS_OTLP_ENDPOINT` adds a
second span exporter to the platform collector so Grafana and the trace store see the same
traces without a second instrumentation."""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _resource(service_name: str) -> Resource:
    return Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "argus",
            "service.version": os.getenv("SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.getenv("DEPLOY_ENV", "local"),
        }
    )


def configure_strands(service_name: str):
    """Configure Strands' own telemetry (traces + metrics). Returns the StrandsTelemetry object."""
    from strands.telemetry import StrandsTelemetry

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        f"service.namespace=argus,deployment.environment={os.getenv('DEPLOY_ENV', 'local')}",
    )
    telemetry = StrandsTelemetry()
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        telemetry.setup_otlp_exporter().setup_meter(enable_otlp_exporter=True)
    elif os.getenv("OTEL_CONSOLE", "false").lower() == "true":
        telemetry.setup_console_exporter()
    _fan_out(
        telemetry.tracer_provider if hasattr(telemetry, "tracer_provider") else None
    )
    _instrument_http()
    return telemetry


def configure_langchain(service_name: str) -> None:
    """Generic OTel provider plus OpenInference instrumentation for LangChain/LangGraph."""
    if os.getenv("AGENT_OBSERVABILITY_ENABLED", "false").lower() == "true":
        # ADOT already installed the global provider (unified telemetry to CloudWatch):
        # instrument into it and add our fan-out instead of building a second provider
        # whose exporter thread would idle for the life of the process.
        provider = trace.get_tracer_provider()
    else:
        provider = TracerProvider(resource=_resource(service_name))
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
    from openinference.instrumentation.langchain import LangChainInstrumentor

    LangChainInstrumentor().instrument(tracer_provider=provider)
    _fan_out(provider)
    _instrument_http()


def _fan_out(provider) -> None:
    """Second span exporter to the platform collector (`ARGUS_OTLP_ENDPOINT`), for the
    Grafana/Tempo/the trace store view, next to whatever the primary pipeline does."""
    endpoint = os.getenv("ARGUS_OTLP_ENDPOINT")
    if not endpoint:
        return
    target = provider or trace.get_tracer_provider()
    if not hasattr(target, "add_span_processor"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    target.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        )
    )


def _instrument_http() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
