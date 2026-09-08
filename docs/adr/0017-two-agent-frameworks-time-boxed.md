# ADR-0017: Two agent frameworks, on purpose and time-boxed

Date: 2026-09-07. Status: accepted, review by 2026-12-01.

## Context

Three agents (Watch, Tasking, the orchestrator's report node) are Strands agents; the
Investigator is a LangGraph graph. The README gives the demonstration rationale: the
platform is meant to show that the AgentCore plane (runtime, gateway, memory,
evaluations, observability) is framework-neutral. That choice has a price: two
sets of model adapters and telemetry glue in `agents/shared/`, three A2A servers, two
dependency trees to keep in step, and two places a Bedrock or guardrail change must be
made.

## Decision

- Keep both frameworks for now; the neutrality claim is part of what the demo proves
  and the Investigator's tool-heavy loop is where LangGraph's checkpointing (ADR-0016)
  will land first.
- Contain the cost: model construction stays in `shared/models.py` only
  (`strands_model` / `langchain_model`), telemetry in `shared/telemetry.py`, tool
  binding in `shared/tools.py`, caller identity in `shared/caller_auth.py`; a change to
  any of these must land for both frameworks in the same commit. Version pins for the
  OpenTelemetry SDK and instrumentations are identical across the per-image
  requirements files (checked in review).
- Time-box it: at the review date, either the Investigator moves to Strands (dropping
  LangGraph and the OpenInference instrumentor) or the three Strands agents move to
  LangGraph, unless a measured reason to keep both is written into this record.

## Consequences

Until the review, every agent-side change costs two code paths, and the manifest's
`framework` field is the only place the difference is visible to an officer. After it,
the platform keeps its framework-neutral plane but the code base pays for one framework.
