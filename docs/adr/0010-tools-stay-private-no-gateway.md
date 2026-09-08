# ADR-0010: Tool access stays private; AgentCore Gateway is not used

Date: 2026-09-05. Status: superseded by ADR-0011 (2026-09-07).

## Context

AgentCore Gateway offers a managed MCP front door with JWT authorisation, rate limits and tool discovery. The repository carried an unused, optional Gateway definition. Argus's four MCP servers run as Fargate tasks behind an internal load balancer, reachable only inside the VPC, and every call carries an STS-signed caller token verified against a per-server role allowlist (ADR-0001).

## Decision

Remove the Gateway definition. The MCP servers stay private behind the internal load balancer with caller identity enforced by the servers themselves.

## Consequences

- Adopting Gateway would have required the MCP servers to be reachable by the AWS-managed service, meaning a public HTTPS endpoint with a certificate, and would have replaced role-based caller identity with JWT client credentials. That widens exposure for tools that read the vessel registry and decrypt personal data.
- Rate limiting and tool discovery are handled in code: the agents bind a fixed tool set at start, and the API's job queue bounds concurrency.
- If a second organisation or an external agent ever needs these tools, Gateway becomes the right answer and this decision should be revisited.
