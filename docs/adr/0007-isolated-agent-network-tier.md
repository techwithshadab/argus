---
status: accepted
---
# Agents run in an isolated subnet tier with no internet route

AgentCore runtimes used to share the private subnets with the ECS services, which have NAT egress for external data feeds. With production pinned to Bedrock (ADR-0002) the agents have no legitimate reason to reach the internet, so they now run in a `PRIVATE_ISOLATED` subnet group with no NAT route at all. Everything they need is an interface endpoint (Bedrock runtime, AgentCore, CloudWatch Logs, X-Ray, Secrets Manager, SSM, STS) or the internal ALB. Their security group has no default egress; it allows 443 to the VPC (endpoints) and 8000-8010 / 4317-4318 to the services group. The ECS services keep NAT egress because AISStream, Copernicus, OSM and OpenSanctions are external.

## Consequences

A prompt-injected or misbehaving agent cannot exfiltrate to the internet even if IAM were misconfigured; the network is the second control. Adding an AWS service to an agent's needs means adding a VPC endpoint, not opening egress. The `bedrock:InvokeModel` grant is limited to first-party foundation models of the allowed vendors (`anthropic`, `amazon` by default) and this account's inference profiles, so Bedrock Marketplace models on SageMaker endpoints cannot be invoked even by a permissive model id.
