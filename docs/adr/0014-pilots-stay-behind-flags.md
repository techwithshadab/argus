# ADR-0014: Pilots stay behind flags and attest what actually ran

Date: 2026-09-07. Status: accepted (refines ADR-0013).

## Context

ADR-0013 added two pilots: the Tasking node through an AgentCore Harness
(`taskingViaHarness`) and the orchestrator's configuration bundle, whose
`report_system_prompt` the report node preferred whenever a request carried a bundle.
Left ungated, the second pilot leaks into the production path: any request with a bundle
in its baggage changes the report prompt, the bundle is fetched on every report call, the
manifest hashes the managed prompt rather than the prompt that ran, and a `model_id`
field in the bundle is read by nothing.

## Decision

- Every pilot is opt-in by a deploy-time flag and off by default. The harness stays
  behind `taskingViaHarness`; the bundle override is behind `bundleOverride`
  (`BUNDLE_OVERRIDE` on the orchestrator runtime, `settings.bundle_override`).
- The manifest attests the prompt that actually ran: `prompt` is the hash of the text
  used and `prompt_version` is either the managed version or `bundle:<version>`
  (`choose_report_prompt` in `agents/shared/graph.py`, pure and unit-tested).
- The bundle carries only what the code reads (`report_system_prompt`).
- A pilot is promoted only after `evals/node_evals.py --gate` passes with the flag on
  (ADR-0009), and then by making the flag the default in `cdk.json`, not by removing it.

## Consequences

An A/B run or a recommendation can still attach a bundle version to a request, but only
a deploy that turned the flag on will honour it, and the officer's report manifest says
which text produced the report. The harness path remains unevaluated until its gate run.
