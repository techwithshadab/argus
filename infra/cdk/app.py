#!/usr/bin/env python3
"""CDK app: four stacks, deployed in order by `cdk deploy --all`.

argus-network   VPC, subnets, security groups, VPC endpoints
argus-data      Aurora Serverless v2 PostgreSQL (PostGIS), ElastiCache Serverless (Valkey), secrets
argus-platform  ECS Fargate: MCP servers, API, UI, AIS replay, ADOT collector; internal + public ALBs
argus-agents    Bedrock AgentCore: Memory, 4 Runtimes (3 x A2A specialists, 1 x HTTP orchestrator),
              optional Gateway; IAM; SSM parameter with the orchestrator runtime ARN

Lifecycle (see stacks/lifecycle.py): `-c paused=true` scales every ECS service to zero and lets
Aurora auto-pause; `cdk destroy --all` removes everything the app created.
"""

import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks import nag
from stacks.agents_stack import AgentsStack
from stacks.data_stack import DataStack
from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack

app = cdk.App()
env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION", "us-east-1"),
)
name = app.node.try_get_context("projectName") or "argus"

network = NetworkStack(app, f"{name}-network", env=env)
data = DataStack(
    app, f"{name}-data", vpc=network.vpc, services_sg=network.services_sg, env=env
)
platform = PlatformStack(
    app,
    f"{name}-platform",
    vpc=network.vpc,
    services_sg=network.services_sg,
    agents_sg=network.agents_sg,
    nat_gateway_ids=network.nat_gateway_ids,
    data=data,
    env=env,
)
agents = AgentsStack(
    app,
    f"{name}-agents",
    vpc=network.vpc,
    agent_subnets=network.agent_subnets,
    agents_sg=network.agents_sg,
    services_sg=network.services_sg,
    platform=platform,
    data=data,
    env=env,
)

for s in (network, data, platform, agents):
    cdk.Tags.of(s).add("project", name)
    cdk.Tags.of(s).add("demo", "dark-vessel")
# cdk-nag (AWS Solutions rules) on every stack; findings fail synth unless suppressed with
# a reason next to the resource (stacks/nag.py). -c nag=false skips the checks.
if str(app.node.try_get_context("nag") or "true").lower() != "false":
    nag.network(network)
    nag.data(data)
    nag.platform(platform, grafana=platform.grafana_stack)
    nag.agents(agents)
    cdk.Aspects.of(app).add(AwsSolutionsChecks())
app.synth()
