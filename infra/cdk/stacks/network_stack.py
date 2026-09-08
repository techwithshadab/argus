"""Network: one VPC with three subnet tiers and the endpoints that keep traffic off the internet.

public    ALB for the UI only
private   ECS services (MCP servers, API, collector, replay). NAT egress for external data feeds
          (AISStream, Copernicus, OSM, OpenSanctions) and public image pulls.
agents    AgentCore runtimes. Isolated: no NAT route at all. Everything they need (Bedrock,
          AgentCore, CloudWatch, X-Ray, Secrets Manager, SSM, STS) is an interface endpoint, and
          the MCP servers and API are reached over the internal ALB. Model traffic cannot leave
          the account (ADR-0002, ADR-0007)."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

AGENT_SUBNET_GROUP = "agents"


class NetworkStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw):
        super().__init__(scope, cid, **kw)
        # One NAT gateway per zone (default) so a zone loss does not take the feeds and
        # image pulls of the other zone with it; -c natPerAz=false keeps a single one
        # (about $33/month less, including while paused).
        per_az = str(self.node.try_get_context("natPerAz") or "true").lower() != "false"
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=2 if per_az else 1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                ),
                ec2.SubnetConfiguration(
                    name=AGENT_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )
        # The deploy identity, so nothing after the first deploy needs root: administrator
        # rights, assumable by -c deployerPrincipalArn (an IAM user or an Identity Center
        # permission set) or, by default, by any principal in the account that signed in
        # with MFA. Put it in an AWS profile (docs/RUNBOOK.md, "Deploying without root").
        deployer = self.node.try_get_context("deployerPrincipalArn") or ""
        iam.Role(
            self,
            "Deployer",
            role_name="argus-deployer",
            assumed_by=(
                iam.ArnPrincipal(deployer)
                if deployer
                else iam.AccountPrincipal(self.account).with_conditions(
                    {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
                )
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            ],
            max_session_duration=Duration.hours(4),
            description="Deploys Argus with CDK (assume with MFA instead of using root)",
        )
        self.agent_subnets = self.vpc.select_subnets(
            subnet_group_name=AGENT_SUBNET_GROUP
        ).subnets
        # The platform stack alarms on each gateway's port-allocation and drop counters.
        self.nat_gateway_ids = [
            n.ref for n in self.vpc.node.find_all() if isinstance(n, ec2.CfnNatGateway)
        ]

        # Flow logs: every accepted and rejected connection, kept a month, deleted with the stack.
        flow_logs = logs.LogGroup(
            self,
            "FlowLogs",
            log_group_name="/argus/vpc-flow-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # Keep model, control-plane, telemetry and image traffic off the NAT gateway and reachable
        # from the isolated agent subnets. Endpoint ENIs live in the private subnets; private DNS
        # resolves the service names for every subnet in the VPC.
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        for n, svc in {
            "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDkr": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "Ssm": ec2.InterfaceVpcEndpointAwsService.SSM,
            "Secrets": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "Sts": ec2.InterfaceVpcEndpointAwsService.STS,
            "Xray": ec2.InterfaceVpcEndpointAwsService.XRAY,
            "Bedrock": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "AgentCore": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
        }.items():
            self.vpc.add_interface_endpoint(n, service=svc)
        # The agents reach the AgentCore Gateway (their only tool endpoint) and the Agent
        # Registry through interface endpoints too; both publish private DNS names.
        for n, svc in {
            "AgentCoreGateway": f"com.amazonaws.{self.region}.bedrock-agentcore.gateway",
            "AgentRegistry": f"com.amazonaws.{self.region}.agent-registry",
            # Bedrock Prompt Management (GetPrompt) is on the bedrock-agent control plane.
            "BedrockAgent": f"com.amazonaws.{self.region}.bedrock-agent",
        }.items():
            # InterfaceVpcEndpointService (unlike the AwsService enum) leaves private DNS
            # off; without it the gateway hostname resolves to public addresses that the
            # isolated agent subnets cannot reach, and every agent hangs binding its tools.
            self.vpc.add_interface_endpoint(
                n,
                service=ec2.InterfaceVpcEndpointService(svc, 443),
                private_dns_enabled=True,
            )

        # ---- security groups ----
        self.services_sg = ec2.SecurityGroup(
            self,
            "ServicesSg",
            vpc=self.vpc,
            description="ECS services (MCP, API, UI, collector) and the internal ALB",
            allow_all_outbound=True,  # external data feeds via NAT
        )
        self.agents_sg = ec2.SecurityGroup(
            self,
            "AgentsSg",
            vpc=self.vpc,
            description="AgentCore runtimes (VPC mode, isolated subnets)",
            allow_all_outbound=False,
        )
        vpc_cidr = ec2.Peer.ipv4(self.vpc.vpc_cidr_block)
        # Rules are CIDR-based where possible so downstream stacks can import the groups immutably
        # (no cross-stack security-group cycles).
        self.services_sg.add_ingress_rule(
            vpc_cidr,
            ec2.Port.tcp_range(8000, 8010),
            "internal ALB and Service Connect: MCP servers and API",
        )
        self.services_sg.add_ingress_rule(
            vpc_cidr, ec2.Port.tcp_range(4317, 4318), "OTLP to the collector"
        )
        # Agents may only talk to the internal ALB/services and to interface endpoints on 443.
        self.agents_sg.add_egress_rule(
            self.services_sg,
            ec2.Port.tcp_range(8000, 8010),
            "agents to internal ALB / MCP / API",
        )
        self.agents_sg.add_egress_rule(
            self.services_sg, ec2.Port.tcp_range(4317, 4318), "agents to OTel collector"
        )
        self.agents_sg.add_egress_rule(
            vpc_cidr, ec2.Port.tcp(443), "agents to VPC interface endpoints"
        )
        # AgentCore pulls the runtime image layers from ECR's S3 bucket through the S3 gateway
        # endpoint, whose traffic goes to S3's public prefix list, not the VPC CIDR.
        s3_prefix_list = ec2.PrefixList.from_lookup(
            self, "S3PrefixList", prefix_list_name=f"com.amazonaws.{self.region}.s3"
        )
        self.agents_sg.add_egress_rule(
            ec2.Peer.prefix_list(s3_prefix_list.prefix_list_id),
            ec2.Port.tcp(443),
            "agents to S3 via the gateway endpoint (ECR image layers)",
        )
