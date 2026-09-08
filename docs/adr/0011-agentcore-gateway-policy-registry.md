# ADR-0011: Tools behind AgentCore Gateway with Policy; the catalog in AWS Agent Registry

Date: 2026-09-07. Status: accepted. Supersedes ADR-0010.

## Context

ADR-0010 kept the four MCP servers private on ECS behind an internal load balancer and had
every caller prove its IAM role with an STS-signed token that each server verified against
an allowlist. That worked, but the authorization lived in our code, tool discovery was
static configuration, and nothing outside the repository described what this deployment
ran. Since then AWS shipped the pieces that make those our problems no longer: AgentCore
Gateway fronts MCP servers with IAM inbound auth and semantic tool search, Policy in
AgentCore evaluates Cedar rules on every tool call at the gateway, and AWS Agent Registry
is a managed catalog of MCP servers, agents and skills with an approval workflow.

One constraint shaped the design: the gateway signs outbound calls with IAM only to targets
that verify SigV4 themselves (AgentCore Runtime, API Gateway, Lambda). A load balancer does
not, so the servers cannot stay on ECS behind the gateway.

## Decision

- The four MCP servers run as AgentCore Runtime endpoints (protocol MCP, same image and
  code, arm64) in the private subnets with the services security group, so they keep the
  database, the personal-data key and OpenSanctions egress. A resource policy on each
  runtime denies invocation to every principal except the gateway's role.
- One AgentCore Gateway (`argus-tools`, IAM inbound, semantic search) has the four servers
  as targets. Agents open one MCP session to it, signed with their own runtime role.
- A Policy engine attached to the gateway in `ENFORCE` mode holds one Cedar policy per
  agent role, generated from `mcp-servers/tools.json` (`infra/cdk/stacks/tool_policy.py`):
  watch may call ais and geo, investigator ais, registry and geo, tasking imagery and geo,
  the orchestrator nothing. Default deny, forbid wins; every decision is logged.
- AWS Agent Registry `argus` holds records for the four servers (server.json plus tool
  schemas), the three A2A agents (their cards, from `agents/shared/cards.py`, the same data
  the agents serve) and the orchestrator, created and auto-approved by the deploy.
- AgentCore Evaluations scores live sessions of the four agents online (built-in
  helpfulness, correctness, instruction following, tool selection and parameters, goal
  success, harmfulness, plus the report rubric as a custom evaluator). This needs the
  agents' telemetry in CloudWatch, so the runtimes use AgentCore's unified telemetry and
  CloudWatch Transaction Search is on; a second span exporter feeds the platform collector
  for Grafana.
- CloudWatch keeps only what AgentCore itself writes. The X-Ray, EMF and log fan-outs, the
  CloudWatch dashboard and the CloudWatch alarms are gone; Grafana alerting to SNS pages.

## Consequences

- Authorization for tools is outside agent code and declarative; changing who may call
  what is a policy change validated at deploy against the gateway's tool schema.
- `tools.json` is generated from the servers (`make tools-inventory`) and pinned by a unit
  test, so registry records, Cedar policies and the running servers cannot drift.
- Locally (docker compose) nothing changes: agents call the MCP containers directly.
  `agents/shared/tools.py` hides the two modes.
- The A2A specialist agents are not yet gateway targets (an MCP-protocol gateway cannot
  hold runtime targets); orchestrator-to-agent calls stay IAM-signed to the runtimes.
- The STS caller-token code remains for the API's agent-only routes.

Extended by ADR-0013 (registry as discovery, Identity for feed keys, a gateway in front of the agents, Harness and optimization pilots).
