# ADR-0013: Discovery through the registry, Identity for feed keys, a gateway in front of the agents, Harness and optimization pilots

Date: 2026-09-07. Status: accepted (extends ADR-0011).

## Context

ADR-0011 put the tool servers behind an AgentCore Gateway with a Policy engine and
published records to the AWS Agent Registry, but four AgentCore capabilities were still
unused or half-used:

- the registry was written to and never read: agents found each other through
  environment variables and an SSM parameter;
- the OpenSanctions key reached the registry server as a Secrets Manager ARN, outside
  AgentCore Identity's token vault;
- agent-to-agent calls (orchestrator to Watch, Investigator, Tasking; worker to Watch)
  went straight to the runtimes, so the gateway's logging and policy did not see them;
- AgentCore Harness (the managed agent loop) and optimization (configuration bundles,
  recommendations, A/B tests) were not exercised at all.

## Decision

1. **Registry as discovery.** `agents/shared/discovery.py` (orchestrator) and
   `registry_agent_url` in the worker resolve `argus-agent-<name>` from the registry
   (`ListDiscoverableRegistryRecords` on `ARGUS_REGISTRY_ID`, also published as SSM
   `/argus/registry-id`, then `BatchGetDiscoverableRegistryRecord` for the A2A card and
   its `url`; the listing carries names and status only). Records are created as DRAFT
   and only APPROVED records are discoverable, so a custom resource submits each record
   for approval after creation (`SubmitRegistryRecordForApproval`; the registry's
   APPROVE_ALL rule approves at once). The environment and the SSM parameter are
   fallbacks. Records are the source of truth for where an agent lives.
2. **AgentCore Identity for feed keys.** The OpenSanctions secret is JSON
   (`{"api_key": ...}`) and backs an API-key credential provider (`argus-opensanctions`,
   EXTERNAL source). AgentCore Runtime injects each invocation's workload access token
   (`X-Amz-Bedrock-AgentCore-Identity-WAT`); `common/identity.py` keeps it and the
   registry server exchanges it with `GetResourceApiKey`. The plain secret stays as the
   fallback (local compose, or a runtime without the header).
3. **A second gateway, `argus-agents`.** One AgentCore Runtime target per specialist
   agent (Watch, Investigator, Tasking) on a gateway without a protocol type; callers
   keep signing with IAM (`A2A_AUTH=sigv4`) and the runtime session id header passes
   through. The orchestrator's `A2A_*_URL` and the worker's `/argus/watch-a2a-url`
   point at `<gateway>/<target>/invocations`. The specialist runtimes keep no resource
   policy, so a direct invocation stays possible as a break-glass path.
4. **Harness pilot for Tasking.** `argus_tasking` is an AgentCore Harness (Nova 2 Lite,
   the tasking prompt, the tools gateway with IAM outbound auth, `allowedTools` limited
   to imagery and geo, VPC mode in the agents subnets, its own Cedar policy). The
   orchestrator calls it with `InvokeHarness` only when `TASKING_HARNESS_ARN` is set
   (CDK context `taskingViaHarness=true`); the default stays the Tasking runtime over
   A2A, because the harness has no eval history yet.
5. **Optimization hook.** A configuration bundle (`argus_orchestrator`) carries the
   report prompt and model. The report node reads
   `BedrockAgentCoreContext.get_config_bundle()` and prefers `report_system_prompt`
   from the bundle when a request carries one (recommendations and `agentcore run
   ab-test` attach bundle versions through W3C baggage); otherwise the managed prompt.

## Consequences

- Discovery adds one registry search per process (cached five minutes); a stale or
  unapproved record falls back to the configured URL and logs why.
- The OpenSanctions secret must be stored as JSON; `scripts/deploy.sh` does that. A
  pre-ADR plain-string value is still read (the loader accepts both shapes).
- All agent calls are visible in the gateway's CloudWatch logs; the policy engine
  on `argus-agents` is not enabled (targets are whole agents, not tools), so
  authorization there is IAM only.
- Harness and bundle are pilots: they cost nothing while idle, and the eval gate still
  runs against the A2A path. Promoting the harness needs `evals/node_evals.py --gate`
  with `taskingViaHarness=true`.
- Local compose is unchanged: no registry, no Identity, no gateways (direct mode).
