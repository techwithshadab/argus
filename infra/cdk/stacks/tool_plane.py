"""The tool plane on AgentCore: the four MCP servers as AgentCore Runtime endpoints, one
AgentCore Gateway in front of them, and a Policy engine whose Cedar rules decide, outside
agent code, which agent may call which tool (ADR-0011).

Why runtimes and not the ECS services: the gateway signs outbound calls with IAM only to
services that verify SigV4 themselves (AgentCore Runtime, API Gateway, Lambda); a load
balancer does not. The servers keep the same image and code; only the host changes."""

from __future__ import annotations

import json
from dataclasses import dataclass

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr

from .tool_policy import policies

TOOL_SERVERS = ("ais", "registry", "geo", "imagery")


@dataclass
class AgentsGateway:
    gateway: agentcore.CfnGateway
    gateway_role: iam.Role
    target_urls: dict[str, str]


@dataclass
class ToolPlane:
    gateway: agentcore.CfnGateway
    gateway_url: str
    gateway_role: iam.Role
    runtimes: dict[str, agentcore.CfnRuntime]
    policy_engine: agentcore.CfnPolicyEngine
    policies: dict[str, agentcore.CfnPolicy]


def runtime_invocation_url(stack: Stack, rt: agentcore.CfnRuntime) -> str:
    encoded = (
        f"arn%3Aaws%3Abedrock-agentcore%3A{stack.region}%3A{stack.account}%3Aruntime%2F"
        f"{rt.attr_agent_runtime_id}"
    )
    return (
        f"https://bedrock-agentcore.{stack.region}.amazonaws.com/runtimes/{encoded}"
        "/invocations?qualifier=DEFAULT"
    )


def build_tool_plane(
    stack: Stack,
    *,
    image: ecr_assets.DockerImageAsset,
    subnets: list[str],
    security_group_ids: list[str],
    tool_role: iam.Role,
    env: dict[str, str],
    server_env: dict[str, dict[str, str]],
    inventory: dict,
    agent_roles: dict[str, iam.Role],
) -> ToolPlane:
    # ---- MCP servers on AgentCore Runtime (protocol MCP: port 8000, path /mcp) ----
    runtimes: dict[str, agentcore.CfnRuntime] = {}
    for name in TOOL_SERVERS:
        rt = agentcore.CfnRuntime(
            stack,
            f"ToolRuntime{name.title()}",
            agent_runtime_name=f"argus_tool_{name}",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image.image_uri
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    security_groups=security_group_ids, subnets=subnets
                ),
            ),
            protocol_configuration="MCP",
            role_arn=tool_role.role_arn,
            environment_variables={
                **env,
                **server_env.get(name, {}),
                "MCP_SERVER": name,
                "MCP_PORT": "8000",
                "OTEL_SERVICE_NAME": f"mcp-{name}",
            },
            description=f"Argus {name} MCP server",
        )
        rt.node.add_dependency(tool_role)
        # AgentCore creates the runtime's log group itself, so nothing in the stack owns
        # it and `cdk destroy` left one behind per tool server. Declaring an
        # `AWS::Logs::LogGroup` for these is forbidden (it fights AgentCore), so the
        # deletion goes through the SDK, exactly as the agent runtimes do (I6).
        cr.AwsCustomResource(
            stack,
            f"ToolLogs{name.title()}",
            on_delete=cr.AwsSdkCall(
                service="CloudWatchLogs",
                action="deleteLogGroup",
                parameters={
                    "logGroupName": (
                        f"/aws/bedrock-agentcore/runtimes/{rt.attr_agent_runtime_id}-DEFAULT"
                    )
                },
                # Without this a failed rollback wedges the stack.
                ignore_error_codes_matching=".*",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["logs:DeleteLogGroup"],
                        resources=[
                            f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/*",
                            f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/*:*",
                        ],
                    )
                ]
            ),
        ).node.add_dependency(rt)
        runtimes[name] = rt

    # ---- gateway execution role: may invoke exactly the tool runtimes ----
    gateway_role = iam.Role(
        stack,
        "GatewayRole",
        role_name="argus-tool-gateway",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": stack.account},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/*"
                },
            },
        ),
        description="AgentCore Gateway role for the Argus tool servers",
    )
    gateway_role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[
                arn
                for rt in runtimes.values()
                for arn in (
                    rt.attr_agent_runtime_arn,
                    f"{rt.attr_agent_runtime_arn}/runtime-endpoint/*",
                )
            ],
        )
    )
    # The documented set for a gateway execution role with Policy attached
    # (devguide/policy-permissions): GetPolicyEngine on the engine, AuthorizeAction and
    # PartiallyAuthorizeActions on both the engine and the gateway. CreateGateway checks
    # them up front ("Access denied while calling GetPolicyEngine", then "not authorized
    # to perform AuthorizeAction") and the whole stack rolls back when one is missing.
    engines = (
        f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:policy-engine/*"
    )
    gateways = f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/*"
    gateway_role.add_to_policy(
        iam.PolicyStatement(
            sid="PolicyEngineConfiguration",
            actions=["bedrock-agentcore:GetPolicyEngine"],
            resources=[engines],
        )
    )
    gateway_role.add_to_policy(
        iam.PolicyStatement(
            sid="PolicyEngineAuthorization",
            actions=[
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            resources=[engines, gateways],
        )
    )
    # Only the gateway may reach a tool runtime: everything else is denied at the resource,
    # so the policy engine cannot be bypassed by calling a server directly.
    for name, rt in runtimes.items():
        agentcore.CfnResourcePolicy(
            stack,
            f"ToolRuntimePolicy{name.title()}",
            resource_arn=rt.attr_agent_runtime_arn,
            # The documented shape (devguide runtime-oauth, "Restrict IAM (SigV4) inbound
            # invocation to your gateway"): allow the gateway role, deny every other
            # principal, Resource always the runtime's own ARN ("*" is rejected).
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowOnlyGatewayRole",
                            "Effect": "Allow",
                            "Principal": {"AWS": gateway_role.role_arn},
                            "Action": "bedrock-agentcore:InvokeAgentRuntime",
                            "Resource": rt.attr_agent_runtime_arn,
                        },
                        {
                            "Sid": "DenyOtherPrincipals",
                            "Effect": "Deny",
                            "Principal": {"AWS": "*"},
                            "Action": "bedrock-agentcore:InvokeAgentRuntime",
                            "Resource": rt.attr_agent_runtime_arn,
                            "Condition": {
                                "ArnNotEquals": {
                                    "aws:PrincipalArn": gateway_role.role_arn
                                }
                            },
                        },
                    ],
                }
            ),
        ).node.add_dependency(gateway_role)

    # ---- policy engine (Cedar, default deny, forbid wins) ----
    engine = agentcore.CfnPolicyEngine(
        stack,
        "ToolPolicyEngine",
        name="argus_tools",
        description="Which Argus agent may call which tool",
    )

    # ---- the gateway: IAM inbound, semantic tool search, policies enforced ----
    gateway = agentcore.CfnGateway(
        stack,
        "ToolGateway",
        name="argus-tools",
        description="Argus tool servers (AIS, registry, geo, imagery) behind one MCP endpoint",
        role_arn=gateway_role.role_arn,
        authorizer_type="AWS_IAM",
        protocol_type="MCP",
        protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
            mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                search_type="SEMANTIC",
                instructions="Maritime domain awareness tools: AIS tracks and detectors, vessel registry and sanctions, geography and zones, imagery archive and tasking.",
            )
        ),
        policy_engine_configuration=agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
            arn=engine.attr_policy_engine_arn, mode="ENFORCE"
        ),
        exception_level="DEBUG",
    )
    gateway.node.add_dependency(gateway_role)
    gateway.node.add_dependency(engine)

    targets = []
    for name, rt in runtimes.items():
        t = agentcore.CfnGatewayTarget(
            stack,
            f"ToolTarget{name.title()}",
            gateway_identifier=gateway.attr_gateway_identifier,
            name=name,
            description=f"Argus {name} MCP server on AgentCore Runtime",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=agentcore.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=runtime_invocation_url(stack, rt),
                        listing_mode="DEFAULT",
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                    credential_provider=agentcore.CfnGatewayTarget.CredentialProviderProperty(
                        iam_credential_provider=agentcore.CfnGatewayTarget.IamCredentialProviderProperty(
                            service="bedrock-agentcore", region=stack.region
                        )
                    ),
                )
            ],
        )
        t.node.add_dependency(rt)
        t.node.add_dependency(gateway)
        targets.append(t)

    # ---- one Cedar policy per agent role, validated against the synced tool schema ----
    cedar = policies(inventory, stack.account, gateway.attr_gateway_arn)
    pols: dict[str, agentcore.CfnPolicy] = {}
    for role, statement in cedar.items():
        p = agentcore.CfnPolicy(
            stack,
            f"ToolPolicy{role.title()}",
            name=f"argus_{role}_tools",
            description=f"Tools the {role} agent may call",
            policy_engine_id=engine.attr_policy_engine_id,
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=statement)
            ),
            enforcement_mode="ACTIVE",
            validation_mode="FAIL_ON_ANY_FINDINGS",
        )
        for t in targets:
            p.node.add_dependency(t)
        pols[role] = p

    for role in agent_roles.values():
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[gateway.attr_gateway_arn],
            )
        )

    engine.apply_removal_policy(RemovalPolicy.DESTROY)
    return ToolPlane(
        gateway=gateway,
        gateway_url=gateway.attr_gateway_url,
        gateway_role=gateway_role,
        runtimes=runtimes,
        policy_engine=engine,
        policies=pols,
    )


def build_agents_gateway(
    stack: Stack,
    runtimes: dict[str, agentcore.CfnRuntime],
    callers: list[iam.Role],
) -> AgentsGateway:
    """A second gateway (`argus-agents`) with one AgentCore Runtime target per specialist
    agent (ADR-0013). Runtime targets need a gateway without a protocol type; each target
    is reached at `<gateway url>/<target>/invocations` and forwards the A2A request, the
    runtime session id header included, signed with the gateway's own role. Callers pass
    IAM (SigV4) exactly as they did for the runtimes, so `A2A_AUTH=sigv4` is unchanged."""
    role = iam.Role(
        stack,
        "AgentsGatewayRole",
        role_name="argus-agents-gateway",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": stack.account},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/*"
                },
            },
        ),
        description="AgentCore Gateway role in front of the Argus specialist agents",
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[
                arn
                for rt in runtimes.values()
                for arn in (
                    rt.attr_agent_runtime_arn,
                    f"{rt.attr_agent_runtime_arn}/runtime-endpoint/*",
                )
            ],
        )
    )
    gateway = agentcore.CfnGateway(
        stack,
        "AgentsGateway",
        name="argus-agents",
        description="Argus specialist agents (Watch, Investigator, Tasking) as runtime targets",
        role_arn=role.role_arn,
        authorizer_type="AWS_IAM",
        exception_level="DEBUG",
    )
    gateway.node.add_dependency(role)
    urls: dict[str, str] = {}
    for name, rt in runtimes.items():
        t = agentcore.CfnGatewayTarget(
            stack,
            f"AgentTarget{name.title()}",
            gateway_identifier=gateway.attr_gateway_identifier,
            name=name,
            description=f"Argus {name} agent (A2A) on AgentCore Runtime",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                http=agentcore.CfnGatewayTarget.HttpTargetConfigurationProperty(
                    agentcore_runtime=agentcore.CfnGatewayTarget.RuntimeTargetConfigurationProperty(
                        arn=rt.attr_agent_runtime_arn, qualifier="DEFAULT"
                    )
                )
            ),
            # No credential provider: a runtime target is signed with the gateway's own
            # role ("IamCredentialProvider is not supported for this target type").
        )
        t.node.add_dependency(rt)
        t.node.add_dependency(gateway)
        urls[name] = f"{gateway.attr_gateway_url}/{name}/invocations"
    for caller in callers:
        caller.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[gateway.attr_gateway_arn, f"{gateway.attr_gateway_arn}/*"],
            )
        )
    return AgentsGateway(gateway=gateway, gateway_role=role, target_urls=urls)
