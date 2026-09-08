---
status: accepted
---
# The investigation workflow is a code-owned graph, not an LLM-chosen sequence

The Orchestrator was a Strands agent whose system prompt asked it to call the Investigator, then Tasking, then write a report from its own transcript. Order was enforced only by prose, nothing ran in parallel, and the report was a second free-form generation over the whole chat. Decision: the workflow is a graph defined in code (LangGraph or Strands Graph): Investigator branches (identity and sanctions in parallel with behaviour analysis) fan out and join, Tasking runs on the extracted evidence gap, and a report node receives only the validated JSON findings. LLM judgement stays inside nodes; edges, timeouts, retries and escalation are code.

## Considered options

- LLM-owned with post-hoc validation: flexible for new specialists, but not replayable and hard to audit.
- Hybrid with an LLM-chosen optional set: machinery for specialists that do not exist yet.

## As built (phase 4)

`agents/orchestrator/app.py` is the graph: two Investigator branches (`Scope: identity` and `Scope: behaviour`) run in parallel over A2A, are merged by a pure function (`shared/graph.py`: the owning branch wins each field, lists are unioned, confidence is the lower), the Tasking agent receives the extracted evidence gap, and a tool-less report node (strong tier) produces the Vessel of Interest report from the merged findings and tasking JSON only, never from a transcript. The report is checked by `shared/policy.py` (allowed actions, evidence traceability) and retried once with the violations; a second failure fails the investigation. Progress for every node is reported to the platform. The Orchestrator no longer has an LLM in the loop except in the report node.

## Consequences

The Orchestrator becomes a thin coordinator; its only LLM work is the report node. Every edge is a span with a known name, so provenance and per-node evals attach naturally. Adding a specialist is a graph change reviewed in code, not a prompt edit.
