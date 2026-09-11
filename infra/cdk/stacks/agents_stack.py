"""Bedrock AgentCore stack: Memory, four Runtimes, a Bedrock Guardrail, IAM and the SSM handoff to the API.

Runtimes run in VPC mode so they reach the MCP servers and API over the internal ALB. The three
specialists use protocol A2A (AgentCore serves them on port 9000); the orchestrator uses HTTP
(/invocations on 8080) and is what the API invokes with InvokeAgentRuntime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from aws_cdk import CfnJson, CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from .assets import context_excludes
from .data_stack import DataStack
from .evaluations import build_evaluations
from .lifecycle import allowed_model_vendors, validate_model_choice
from .platform_stack import PlatformStack
from .prompts import build_prompts
from .registry import build_registry
from .tool_plane import build_agents_gateway, build_tool_plane
from .tool_policy import cedar_permit, load_inventory, tool_ids

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def cards_prompt_text(role: str) -> str:
    """A prompt file's text with the deploy's scenario clock filled in (str.format)."""
    from .prompt_files import prompt_files

    text = prompt_files(REPO_ROOT)[role].read_text()
    try:
        return text.format(scenario_end="{scenario_end}", schema="{schema}")
    except (KeyError, IndexError):
        return text


def load_cards():
    """`agents/shared/cards.py` as a standalone module (the agents package pulls in the
    frameworks; the cards are plain data shared with the registry records)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "argus_cards", Path(REPO_ROOT) / "agents" / "shared" / "cards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        vpc: ec2.Vpc,
        agent_subnets: list[ec2.ISubnet],
        agents_sg: ec2.SecurityGroup,
        services_sg: ec2.SecurityGroup,
        platform: PlatformStack,
        data: DataStack,
        **kw,
    ):
        super().__init__(scope, cid, **kw)
        # Model provider: bedrock (default) | anthropic | openai | gemini. For non-Bedrock
        # providers pass modelApiKeySecretArn (a Secrets Manager secret holding the API key).
        model_provider = self.node.try_get_context("modelProvider") or "bedrock"
        model_id = self.node.try_get_context("modelId") or (
            self.node.try_get_context("bedrockModelId") or "us.amazon.nova-pro-v1:0"
            if model_provider == "bedrock"
            else ""
        )
        api_key_secret_arn = self.node.try_get_context("modelApiKeySecretArn") or ""
        validate_model_choice(self, model_provider, model_id)
        scenario_end = (
            self.node.try_get_context("scenarioEnd") or "2026-09-01T08:00:00Z"
        )
        # Isolated subnets: no NAT route, so nothing an agent does can reach the internet.
        # AgentCore VPC mode only supports some availability zones (by zone id, which maps to
        # a different zone name in every account). deploy.sh resolves `agentcoreZoneIds` to
        # this account's names and passes them as `agentcoreAzs`; subnets in other zones are
        # left out. If nothing matches (another region), every agent subnet is used.
        allowed_azs = {
            z.strip()
            for z in str(self.node.try_get_context("agentcoreAzs") or "").split(",")
            if z.strip()
        }
        chosen = [s for s in agent_subnets if s.availability_zone in allowed_azs]
        subnets = [s.subnet_id for s in (chosen or agent_subnets)]
        # Bedrock on-demand models only: first-party foundation models of the allowed vendors in
        # any region (cross-region inference profiles fan out) plus this account's inference
        # profiles. Marketplace models live on SageMaker endpoints and are simply not grantable here.
        model_arns = [
            f"arn:aws:bedrock:*::foundation-model/{vendor}.*"
            for vendor in allowed_model_vendors(self)
        ] + [f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"]

        # ---- one execution role per agent, least privilege (ADR-0001) ----
        # Role names are deterministic because the MCP servers and API allow callers by role
        # name (TOOL_ALLOWED_ROLES) after STS confirms the identity.
        def agent_role(name: str) -> iam.Role:
            r = iam.Role(
                self,
                f"Role{name.title()}",
                role_name=f"argus-agent-{name}",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                description=f"Execution role for the Argus {name} agent",
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchCheckLayerAvailability",
                    ],
                    resources=[
                        f"arn:aws:ecr:{self.region}:{self.account}:repository/*"
                    ],
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=["ecr:GetAuthorizationToken"], resources=["*"]
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogGroups",
                        "logs:DescribeLogStreams",
                    ],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"
                    ],
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "xray:GetSamplingRules",
                        "xray:GetSamplingTargets",
                        "xray:PutSpans",
                        "xray:PutSpansForIndexing",
                        "cloudwatch:PutMetricData",
                    ],
                    resources=["*"],
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=model_arns,
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"
                    ],
                )
            )
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    ],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/argus_{name}-*",
                    ],
                )
            )
            # VPC mode: the service creates ENIs in our subnets on our behalf.
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "ec2:CreateNetworkInterface",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DeleteNetworkInterface",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeVpcs",
                        "ec2:CreateTags",
                        "ec2:DescribeDhcpOptions",
                        "ec2:DescribeRouteTables",
                    ],
                    resources=["*"],
                )
            )
            # sts:GetCallerIdentity needs no permission; it is how the agent proves who it is
            # to the MCP servers and the API (agents/shared/caller_auth.py).
            if api_key_secret_arn:
                r.add_to_policy(
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[api_key_secret_arn],
                    )
                )
            return r

        roles = {
            n: agent_role(n)
            for n in ("watch", "investigator", "tasking", "orchestrator")
        }

        # ---- Memory: long-term semantic memory per vessel ----
        mem_role = iam.Role(
            self,
            "MemoryRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        mem_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=model_arns,
            )
        )
        memory = agentcore.CfnMemory(
            self,
            "VesselMemory",
            name="argus_vessel_memory",
            event_expiry_duration=90,
            memory_execution_role_arn=mem_role.role_arn,
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="vessel_assessments",
                        namespaces=["/argus/vessel/{actorId}"],
                    )
                )
            ],
        )

        git_sha = self.node.try_get_context("gitSha") or os.getenv("GIT_SHA", "unknown")

        # ---- images (arm64 is mandatory for AgentCore Runtime) ----
        def image(agent: str, reqs: str) -> ecr_assets.DockerImageAsset:
            return ecr_assets.DockerImageAsset(
                self,
                f"Img{agent.title()}",
                directory=REPO_ROOT,
                file="agents/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
                build_args={"AGENT": agent, "REQS": reqs, "GIT_SHA": git_sha},
                exclude=context_excludes(
                    REPO_ROOT,
                    [
                        "agents/Dockerfile",
                        "agents/entrypoint.sh",
                        "agents/shared",
                        f"agents/{agent}",
                        f"agents/{reqs}",
                    ],
                ),
            )

        # ---- Bedrock Guardrail: off-mission topics and harmful content on every model call.
        # No PII masking: reports must name owners and listed persons (product need).
        # Calibration (measured on AWS, see ARCHITECTURE.md): the prompt-attack filter reads
        # the whole prompt and ours are machine-built (tool results are not evaluated by it),
        # and the misconduct filter would block reports about smuggling and sanctions
        # evasion, which is the product. At HIGH the guardrail blocked ~3% of ordinary calls.
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name="argus-agents",
            description="Argus agents: block prompt attacks, off-mission topics and harmful content",
            blocked_input_messaging="This request was blocked by the Argus guardrail.",
            blocked_outputs_messaging="This response was blocked by the Argus guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength="HIGH", output_strength="HIGH"
                    )
                    for t in ("HATE", "INSULTS", "SEXUAL")
                ]
                + [
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="VIOLENCE",
                        input_strength="MEDIUM",
                        output_strength="MEDIUM",
                    ),
                    # Suspected smuggling and sanctions evasion are what reports describe.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="MISCONDUCT", input_strength="LOW", output_strength="NONE"
                    ),
                    # Applied to machine-built prompts; tool text is handled by untrusted().
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="LOW",
                        output_strength="NONE",
                    ),
                ]
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="weapons-targeting",
                        definition="Instructions for attacking, boarding by force, disabling or targeting a vessel, crew or facility with weapons.",
                        examples=["How should we fire on this vessel?"],
                        type="DENY",
                    ),
                    # No "personal surveillance" topic: on live data the topic classifier
                    # blocked one Investigator prompt in five ("retrieve the vessel's track
                    # ... who controls it") despite an explicit vessel carve-out in the
                    # definition, and a blocked prompt costs both branches. Personal data
                    # stays protected by encryption and the registry tool alone.
                ]
            ),
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY")
                ]
            ),
        )
        # A guardrail version is a snapshot: runtimes keep using the old one until a new
        # version resource is created. The description carries a hash of the policy, so any
        # change to the filters or topics replaces the version (Description is create-only).
        policy_hash = hashlib.sha256(
            json.dumps(
                {
                    "content": guardrail.content_policy_config,
                    "topics": guardrail.topic_policy_config,
                    "words": guardrail.word_policy_config,
                },
                default=str,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:10]
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"argus-agents policy {policy_hash}",
        )
        # Retain replaced versions: deleting the old one while runtimes still reference it
        # failed every model call ("guardrail ... does not exist") until the runtimes rolled.
        guardrail_version.apply_removal_policy(RemovalPolicy.RETAIN)

        common_env = {
            "AWS_REGION": self.region,
            "MODEL_PROVIDER": model_provider,
            "MODEL_ID": model_id,
            "SCENARIO_END": scenario_end,
            "AIS_MODE": str(self.node.try_get_context("aisMode") or "replay").lower(),
            "SANCTIONS_SOURCE": "opensanctions"
            if str(self.node.try_get_context("openSanctions") or "").lower() == "true"
            else "local",
            "DEPLOY_ENV": "aws",
            "API_URL": platform.api_url,
            "INTERNAL_CA_SSM": "/argus/internal-ca",  # trust for the API listener (A7)
            # Prove identity to the API with this runtime's own role (agent-only routes).
            "TOOL_AUTH": "aws-iam",
            "BEDROCK_GUARDRAIL_ID": guardrail.attr_guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": guardrail_version.attr_version,
            # AgentCore-native observability: ADOT ships unified telemetry (spans with
            # their payloads) to the runtime's own log group, which AgentCore
            # Observability, Evaluations and Policy read. ARGUS_OTLP_ENDPOINT adds a
            # second span exporter to the platform collector for Grafana.
            "AGENT_OBSERVABILITY_ENABLED": "true",
            "UNIFIED_TRACES_DESTINATION_ENABLED": "true",
            "OTEL_PYTHON_DISTRO": "aws_distro",
            "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
            "ARGUS_OTLP_ENDPOINT": platform.collector_url,
            "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=argus,deployment.environment=aws",
        }
        if api_key_secret_arn:
            common_env["MODEL_API_KEY_SECRET_ARN"] = api_key_secret_arn
        network = agentcore.CfnRuntime.NetworkConfigurationProperty(
            network_mode="VPC",
            network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                security_groups=[agents_sg.security_group_id], subnets=subnets
            ),
        )

        # ---- tool plane: MCP servers on AgentCore Runtime behind one Gateway (ADR-0011) ----
        # The servers need the database, the personal-data key and (registry) OpenSanctions,
        # so they run in the private subnets with the services security group, like the ECS
        # services they replace; agents keep their isolated subnets.
        inventory = load_inventory(Path(REPO_ROOT) / "mcp-servers" / "tools.json")
        tool_image = ecr_assets.DockerImageAsset(
            self,
            "ImgTools",
            directory=REPO_ROOT,
            file="mcp-servers/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
            exclude=context_excludes(REPO_ROOT, ["mcp-servers"]),
        )
        private = [
            sn
            for sn in vpc.private_subnets
            if not allowed_azs or sn.availability_zone in allowed_azs
        ] or vpc.private_subnets
        tool_role = iam.Role(
            self,
            "ToolRole",
            role_name="argus-tools",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                    },
                },
            ),
            description="Execution role for the Argus MCP tool servers on AgentCore Runtime",
        )
        tool_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/*"],
            )
        )
        tool_role.add_to_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        tool_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"
                ],
            )
        )
        tool_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )
        # Identity-side grants only: `grant_read` would also edit the data stack's key
        # policy with this role's ARN and create a data <-> agents dependency cycle.
        tool_secret_arns = [data.db_secret.secret_arn, data.data_key_secret.secret_arn]
        if platform.opensanctions_secret is not None:
            tool_secret_arns.append(platform.opensanctions_secret.secret_arn)
        tool_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=tool_secret_arns
            )
        )
        tool_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"], resources=[data.kms_key.key_arn]
            )
        )
        tool_env = {
            "AWS_REGION": self.region,
            "DEPLOY_ENV": "aws",
            "AIS_MODE": platform.ais_mode,
            # Both the ais and imagery servers read the scenario clock; without it they
            # fall back to a module default that drifts from `-c scenarioEnd` (A7).
            "SCENARIO_END": scenario_end,
            "PGHOST": data.cluster.cluster_endpoint.hostname,
            "PGPORT": "5432",
            "PGDATABASE": "argus",
            "PGUSER": "argus",
            "PGPASSWORD_SECRET_ARN": data.db_secret.secret_arn,
            "DATA_KEY_SECRET_ARN": data.data_key_secret.secret_arn,
            "TOOL_AUTH": "none",  # the gateway is the boundary; see the runtime resource policy
            "OTEL_EXPORTER_OTLP_ENDPOINT": platform.collector_url,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        }
        server_env: dict[str, dict[str, str]] = {
            "geo": {"GEO_USE_OSM": "false"},
            "imagery": {"IMAGERY_OFFLINE": "false"},
            "registry": {},
        }
        if platform.opensanctions_secret is not None:
            server_env["registry"]["OPENSANCTIONS_API_KEY_SECRET_ARN"] = (
                platform.opensanctions_secret.secret_arn
            )
            # AgentCore Identity holds the key as an API-key credential provider that
            # references the platform's secret (EXTERNAL source): the registry runtime proves
            # its workload identity and asks the token vault for the key (ADR-0013).
            provider = agentcore.CfnApiKeyCredentialProvider(
                self,
                "OpenSanctionsCredential",
                name="argus-opensanctions",
                api_key_secret_source="EXTERNAL",
                api_key_secret_config=agentcore.CfnApiKeyCredentialProvider.SecretReferenceProperty(
                    secret_id=platform.opensanctions_secret.secret_arn,
                    json_key="api_key",
                ),
            )
            server_env["registry"]["OPENSANCTIONS_CREDENTIAL_PROVIDER"] = (
                "argus-opensanctions"
            )
            tool_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetResourceApiKey",
                    ],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/*",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default/apikeycredentialprovider/argus-opensanctions",
                    ],
                )
            )
            tool_role.node.add_dependency(provider)
        plane = build_tool_plane(
            self,
            image=tool_image,
            subnets=[sn.subnet_id for sn in private],
            security_group_ids=[services_sg.security_group_id],
            tool_role=tool_role,
            env=tool_env,
            server_env=server_env,
            inventory=inventory,
            agent_roles={n: roles[n] for n in ("watch", "investigator", "tasking")},
        )
        common_env["TOOL_GATEWAY_URL"] = plane.gateway_url
        # ---- Bedrock Prompt Management: one managed, versioned prompt per role (ADR-0012) ----
        prompts = build_prompts(self, REPO_ROOT)
        for role, ref in prompts.items():
            common_env[f"PROMPT_ARN_{role.upper()}"] = ref["arn"]
            common_env[f"PROMPT_VERSION_{role.upper()}"] = ref["version"]
        for r in roles.values():
            r.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:GetPrompt"],
                    resources=[ref["arn"] for ref in prompts.values()]
                    + [f"{ref['arn']}:*" for ref in prompts.values()],
                )
            )

        def runtime(
            name: str, asset: ecr_assets.DockerImageAsset, protocol: str, env: dict
        ) -> agentcore.CfnRuntime:
            rt = agentcore.CfnRuntime(
                self,
                f"Runtime{name.title()}",
                agent_runtime_name=f"argus_{name}",
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=asset.image_uri
                    )
                ),
                network_configuration=network,
                protocol_configuration=protocol,
                role_arn=roles[name].role_arn,
                environment_variables={
                    **env,
                    "OTEL_SERVICE_NAME": f"argus_{name}.DEFAULT",
                },
                description=f"Argus {name} agent",
            )
            rt.node.add_dependency(roles[name])
            # AgentCore creates the runtime log group itself, so an AWS::Logs::LogGroup here
            # collides ("already exists"). Manage retention and removal through SDK calls
            # instead, so destroy still cleans the group up rather than leaving an orphan.
            group = (
                f"/aws/bedrock-agentcore/runtimes/{rt.attr_agent_runtime_id}-DEFAULT"
            )
            retention = cr.AwsCustomResource(
                self,
                f"Logs{name.title()}",
                on_create=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="putRetentionPolicy",
                    parameters={"logGroupName": group, "retentionInDays": 30},
                    physical_resource_id=cr.PhysicalResourceId.of(f"argus-logs-{name}"),
                    ignore_error_codes_matching="ResourceNotFoundException",
                ),
                on_update=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="putRetentionPolicy",
                    parameters={"logGroupName": group, "retentionInDays": 30},
                    physical_resource_id=cr.PhysicalResourceId.of(f"argus-logs-{name}"),
                    ignore_error_codes_matching="ResourceNotFoundException",
                ),
                on_delete=cr.AwsSdkCall(
                    service="CloudWatchLogs",
                    action="deleteLogGroup",
                    parameters={"logGroupName": group},
                    ignore_error_codes_matching=".*",
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement(
                            # DeleteLogGroup on every group in the account was a much
                            # sharper edge than the read beside it (I11).
                            actions=[
                                "logs:PutRetentionPolicy",
                                "logs:DeleteLogGroup",
                            ],
                            resources=[
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*",
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*:*",
                            ],
                        )
                    ]
                ),
            )
            retention.node.add_dependency(rt)
            return rt

        def invocation_url(rt: agentcore.CfnRuntime) -> str:
            # https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded runtime ARN>/invocations?qualifier=DEFAULT
            encoded_arn = f"arn%3Aaws%3Abedrock-agentcore%3A{self.region}%3A{self.account}%3Aruntime%2F{rt.attr_agent_runtime_id}"
            return f"https://bedrock-agentcore.{self.region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"

        strands_img = image("watch", "requirements-strands.txt")
        watch = runtime("watch", strands_img, "A2A", {**common_env, "A2A_PORT": "9000"})
        investigator = runtime(
            "investigator",
            image("investigator", "requirements-langgraph.txt"),
            "A2A",
            {**common_env, "A2A_PORT": "9000"},
        )
        tasking = runtime(
            "tasking",
            image("tasking", "requirements-strands.txt"),
            "A2A",
            {**common_env, "A2A_PORT": "9000"},
        )
        orchestrator = runtime(
            "orchestrator",
            image("orchestrator", "requirements-strands.txt"),
            "HTTP",
            {
                **common_env,
                "PORT": "8080",
                "A2A_AUTH": "sigv4",
                "AGENTCORE_MEMORY_ID": memory.attr_memory_id,
                "A2A_WATCH_URL": invocation_url(watch),
                "A2A_INVESTIGATOR_URL": invocation_url(investigator),
                "A2A_TASKING_URL": invocation_url(tasking),
            },
        )
        for rt in (watch, investigator, tasking):
            orchestrator.node.add_dependency(rt)
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    f"{rt.attr_agent_runtime_arn}*"
                    for rt in (watch, investigator, tasking)
                ],
            )
        )
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:GetEvent",
                ],
                resources=[memory.attr_memory_arn],
            )
        )

        # The worker calls the Watch agent directly for sweeps (phase 3); it reads the URL at runtime.
        # The API reads this at runtime (breaks the platform <-> agents dependency cycle).
        ssm.StringParameter(
            self,
            "OrchestratorArnParam",
            parameter_name="/argus/orchestrator-runtime-arn",
            string_value=orchestrator.attr_agent_runtime_arn,
        )

        # ---- AWS Agent Registry: the catalog of tool servers and agents (ADR-0011) ----
        # ---- a second gateway in front of the specialist agents (ADR-0013) ----
        agents_gw = build_agents_gateway(
            self,
            runtimes={"watch": watch, "investigator": investigator, "tasking": tasking},
            callers=[roles["orchestrator"]],
        )
        for name, url in agents_gw.target_urls.items():
            orchestrator.add_property_override(
                f"EnvironmentVariables.A2A_{name.upper()}_URL", url
            )
        # The worker calls Watch through the agents gateway too; it reads the URL at runtime.
        ssm.StringParameter(
            self,
            "WatchUrlParam",
            parameter_name="/argus/watch-a2a-url",
            string_value=agents_gw.target_urls["watch"],
        )
        CfnOutput(self, "AgentsGatewayUrl", value=agents_gw.gateway.attr_gateway_url)

        cards = load_cards()
        # The cards advertise the agents gateway targets, so discovery through the
        # registry lands on the gateway (every A2A call logged there), never on a runtime.
        a2a_cards = {
            n: cards.a2a_card(n, agents_gw.target_urls[n])
            for n in ("watch", "investigator", "tasking")
        }
        registry = build_registry(
            self,
            inventory=inventory,
            gateway_url=plane.gateway_url,
            agent_cards=a2a_cards,
            custom_records={
                "orchestrator": {
                    **cards.CARDS["orchestrator"],
                    "version": cards.VERSION,
                    "url": invocation_url(orchestrator),
                }
            },
        )
        # The records reference the runtimes' ARNs, so they already depend on them; an
        # explicit registry -> runtime dependency would close a cycle with the
        # orchestrator's role policy and environment, which reference the registry.

        # ---- registry discovery for the orchestrator (records over environment) ----
        orchestrator.add_property_override(
            "EnvironmentVariables.ARGUS_REGISTRY_ID",
            registry.get_att("RegistryId").to_string(),
        )
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/argus/registry-id"
                ],
            )
        )
        for role in roles.values():
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    resources=[
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter/argus/internal-ca"
                    ],
                )
            )
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "agent-registry:SearchDiscoverableRegistryRecords",
                    "agent-registry:ListDiscoverableRegistryRecords",
                    "agent-registry:BatchGetDiscoverableRegistryRecord",
                    # the batch read is authorised per record ("Caller is not authorized
                    # to read the requested records" without these)
                    "agent-registry:GetDiscoverableRegistryRecord",
                    "agent-registry:GetRegistryRecord",
                ],
                resources=[
                    f"arn:aws:agent-registry:{self.region}:{self.account}:registry/*"
                ],
            )
        )
        ssm.StringParameter(
            self,
            "RegistryIdParam",
            parameter_name="/argus/registry-id",
            string_value=registry.get_att("RegistryId").to_string(),
        )

        # ---- AgentCore optimization: a configuration bundle the report node reads ----
        bundle = agentcore.CfnConfigurationBundle(
            self,
            "OrchestratorBundle",
            bundle_name="argus_orchestrator",
            description="Report prompt and model for the orchestrator; recommendations and A/B tests write new versions",
            # the map is keyed by the runtime ARN, a deploy-time token: CfnJson defers it
            components=CfnJson(
                self,
                "OrchestratorBundleComponents",
                value={
                    # CfnJson bypasses CDK's property mapping: CloudFormation key names
                    orchestrator.attr_agent_runtime_arn: {
                        # only the key the report node reads; a model override would be
                        # ignored silently and must go through the tiers instead
                        "Configuration": {
                            "report_system_prompt": cards_prompt_text("report")
                        }
                    }
                },
            ).value,
        )
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetConfigurationBundle",
                    "bedrock-agentcore:GetConfigurationBundleVersion",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:configuration-bundle/*"
                ],
            )
        )
        CfnOutput(self, "OrchestratorBundleArn", value=bundle.attr_bundle_arn)

        # ---- AgentCore Harness pilot: Tasking as a managed agent loop (flag TASKING_HARNESS_ARN) ----
        harness_model = str(
            self.node.try_get_context("harnessModelId") or "us.amazon.nova-2-lite-v1:0"
        )
        harness_role = iam.Role(
            self,
            "HarnessRole",
            role_name="argus-harness-tasking",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role for the Argus tasking harness pilot",
        )
        for stmt in (
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=model_arns,
            ),
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[f"arn:aws:ecr:{self.region}:*:repository/harness-*"],
            ),
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                    "logs:PutResourcePolicy",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "cloudwatch:PutMetricData",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[plane.gateway.attr_gateway_arn],
            ),
        ):
            harness_role.add_to_policy(stmt)
        harness = agentcore.CfnHarness(
            self,
            "TaskingHarness",
            harness_name="argus_tasking",
            execution_role_arn=harness_role.role_arn,
            model=agentcore.CfnHarness.HarnessModelConfigurationProperty(
                bedrock_model_config=agentcore.CfnHarness.HarnessBedrockModelConfigProperty(
                    model_id=harness_model
                )
            ),
            system_prompt=[
                agentcore.CfnHarness.HarnessSystemContentBlockProperty(
                    text=cards_prompt_text("tasking")
                )
            ],
            tools=[
                agentcore.CfnHarness.HarnessToolProperty(
                    type="agentcore_gateway",
                    name="argus-tools",
                    config=agentcore.CfnHarness.HarnessToolConfigurationProperty(
                        agent_core_gateway=agentcore.CfnHarness.HarnessAgentCoreGatewayConfigProperty(
                            gateway_arn=plane.gateway.attr_gateway_arn,
                            outbound_auth=agentcore.CfnHarness.HarnessGatewayOutboundAuthProperty(
                                aws_iam={}
                            ),
                        )
                    ),
                )
            ],
            allowed_tools=["@argus-tools/imagery___*", "@argus-tools/geo___*"],
            memory=agentcore.CfnHarness.HarnessMemoryConfigurationProperty(disabled={}),
            environment=agentcore.CfnHarness.HarnessEnvironmentProviderProperty(
                agent_core_runtime_environment=agentcore.CfnHarness.HarnessAgentCoreRuntimeEnvironmentProperty(
                    network_configuration=agentcore.CfnHarness.NetworkConfigurationProperty(
                        network_mode="VPC",
                        network_mode_config=agentcore.CfnHarness.VpcConfigProperty(
                            security_groups=[agents_sg.security_group_id],
                            subnets=subnets,
                        ),
                    )
                )
            ),
            max_iterations=12,
            timeout_seconds=300,
        )
        harness.node.add_dependency(harness_role)
        harness.node.add_dependency(plane.gateway)
        # The policy engine sees the harness as its execution role: same Cedar shape as
        # the tasking agent, same two servers.
        harness_policy = agentcore.CfnPolicy(
            self,
            "ToolPolicyHarnessTasking",
            name="argus_harness_tasking_tools",
            description="Tools the tasking harness may call",
            policy_engine_id=plane.policy_engine.attr_policy_engine_id,
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                    statement=cedar_permit(
                        f"arn:aws:sts::{self.account}:assumed-role/argus-harness-tasking",
                        plane.gateway.attr_gateway_arn,
                        [a for s in ("imagery", "geo") for a in tool_ids(inventory, s)],
                    )
                )
            ),
            enforcement_mode="ACTIVE",
            validation_mode="FAIL_ON_ANY_FINDINGS",
        )
        for pol in plane.policies.values():
            harness_policy.node.add_dependency(pol)
        roles["orchestrator"].add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeHarness",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=[harness.attr_arn, f"{harness.attr_arn}/*"],
            )
        )
        CfnOutput(self, "TaskingHarnessArn", value=harness.attr_arn)
        # Pilot isolation (ADR-0014): bundle overrides reach the report node only when the
        # deploy says so, so an A/B test or recommendation cannot change production output.
        if str(self.node.try_get_context("bundleOverride") or "").lower() == "true":
            orchestrator.add_property_override(
                "EnvironmentVariables.BUNDLE_OVERRIDE", "true"
            )
        if str(self.node.try_get_context("taskingViaHarness") or "").lower() == "true":
            orchestrator.add_property_override(
                "EnvironmentVariables.TASKING_HARNESS_ARN", harness.attr_arn
            )

        # ---- AgentCore Evaluations: online scoring of the four agents' sessions ----
        judge_model = str(
            self.node.try_get_context("judgeModelId") or "us.amazon.nova-pro-v1:0"
        )
        validate_model_choice(self, "bedrock", judge_model)
        _, online_evals = build_evaluations(
            self,
            agents={
                n: (
                    f"/aws/bedrock-agentcore/runtimes/{rt.attr_agent_runtime_id}-DEFAULT",
                    f"argus_{n}.DEFAULT",
                )
                for n, rt in (
                    ("watch", watch),
                    ("investigator", investigator),
                    ("tasking", tasking),
                    ("orchestrator", orchestrator),
                )
            },
            judge_model_id=judge_model,
            sampling_percentage=float(
                self.node.try_get_context("evalSamplingPercent") or 50.0
            ),
        )
        for n, cfg in online_evals.items():
            cfg.node.add_dependency(
                {
                    "watch": watch,
                    "investigator": investigator,
                    "tasking": tasking,
                    "orchestrator": orchestrator,
                }[n]
            )
        CfnOutput(self, "ToolGatewayUrl", value=plane.gateway_url)
        CfnOutput(self, "RegistryId", value=registry.get_att("RegistryId").to_string())
        CfnOutput(
            self,
            "OnlineEvaluationConfigIds",
            value=",".join(
                c.attr_online_evaluation_config_id for c in online_evals.values()
            ),
        )
        CfnOutput(
            self, "OrchestratorRuntimeArn", value=orchestrator.attr_agent_runtime_arn
        )
        CfnOutput(self, "MemoryId", value=memory.attr_memory_id)
