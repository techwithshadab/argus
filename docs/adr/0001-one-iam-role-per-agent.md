---
status: accepted
---
# One IAM execution role per agent

All four AgentCore runtimes shared a single execution role with wildcard grants (model invoke on every resource, InvokeAgentRuntime on every runtime, memory access on every memory). Argus is now the seed of a production MDA system, so each agent gets its own role scoped to what it does: Watch reaches the ais and geo tools and can only POST alerts; Investigator reaches ais, registry and geo and writes nothing; Tasking reaches imagery and geo and can only create proposed tasking requests; Orchestrator alone may invoke the three specialist runtimes and its one memory. MCP servers and the API authenticate the caller (SigV4 or AgentCore workload-identity JWT) instead of trusting VPC placement.

## Considered options

- Shared role plus network ACLs: cheapest, but a compromised Watch agent could invoke the Orchestrator and write memory.
- Two roles (orchestrator vs specialists): closes the biggest gap but specialists still share tool access.

## How callers are verified

The MCP servers and the API sit behind an internal ALB and have no way to check a SigV4 signature themselves, and AgentCore workload-identity tokens are only verifiable by AgentCore Gateway. So callers prove identity with a signed `sts:GetCallerIdentity` request as a bearer token; the server forwards it to STS and gets the caller's ARN back (the Vault AWS-auth / aws-iam-authenticator pattern). The server holds no secret, tokens expire in 15 minutes and are re-minted per client, and the allowlist is by IAM role name. Rejected alternative: a shared bearer secret per server, because it is one leak away from every agent impersonating every other.

## Consequences

Adding a specialist means adding a role and a tool allowlist; the CDK agents stack becomes a table of agent → permissions. Local compose keeps `A2A_AUTH=none` but MCP servers must accept an authenticated mode, so the auth check is a pluggable middleware, not a code path that only exists on AWS.
