"""AWS Agent Registry: the catalog of what this deployment runs. Records for the four MCP
servers (server.json plus the tool schemas from tools.json), the three A2A agents (their
cards, the same data the agents serve) and the orchestrator. Records are created by the
deploy and auto-approved, so the registry never lags the running system.

CloudFormation has AWS::AgentRegistry::* types but CDK ships no module for them yet, so
these are raw CfnResource declarations against the published schema."""

from __future__ import annotations

import hashlib
import json

from aws_cdk import CfnResource, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr

from .tool_policy import registry_server_descriptor, registry_tools_descriptor


def build_registry(
    stack: Stack,
    *,
    inventory: dict,
    gateway_url: str,
    agent_cards: dict[str, dict],
    custom_records: dict[str, dict],
) -> CfnResource:
    registry = CfnResource(
        stack,
        "Registry",
        type="AWS::AgentRegistry::Registry",
        properties={
            "Name": "argus",
            "Description": "Argus maritime investigation platform: tool servers, agents and skills",
            "AuthorizerType": "AWS_IAM",
            "ApprovalConfiguration": {"AutoApprovalRules": ["APPROVE_ALL"]},
        },
    )
    registry.apply_removal_policy(RemovalPolicy.DESTROY)
    registry_id = registry.get_att("RegistryId").to_string()

    def record(
        cid: str,
        name: str,
        display: str,
        description: str,
        record_type: str,
        descriptors: dict,
    ) -> CfnResource:
        r = CfnResource(
            stack,
            cid,
            type="AWS::AgentRegistry::RegistryRecord",
            properties={
                "RegistryId": registry_id,
                "Name": name,
                "DisplayName": display,
                "Description": description[:1000],
                "RecordType": record_type,
                "RecordVersion": inventory["version"],
                "Descriptors": descriptors,
            },
        )
        r.apply_removal_policy(RemovalPolicy.DESTROY)
        r.node.add_dependency(registry)
        # A record is created as DRAFT and only APPROVED records are discoverable.
        # APPROVE_ALL makes SubmitRegistryRecordForApproval approve it at once; CloudFormation
        # has no property for that step, so a custom resource submits every record on
        # create and whenever the record is replaced (its id changes).
        record_id = r.get_att("RecordId").to_string()
        # The physical id carries the descriptor content, so an updated record (which the
        # registry puts back into review) is submitted again on the next deploy.
        content_hash = hashlib.sha256(
            json.dumps(descriptors, sort_keys=True).encode()
        ).hexdigest()[:12]
        physical_id = f"{record_id}-{content_hash}"
        submit = cr.AwsCustomResource(
            stack,
            f"{cid}Approval",
            install_latest_aws_sdk=True,
            on_create=cr.AwsSdkCall(
                service="@aws-sdk/client-agent-registry-control",
                action="SubmitRegistryRecordForApproval",
                parameters={"registryId": registry_id, "recordId": record_id},
                physical_resource_id=cr.PhysicalResourceId.of(physical_id),
                ignore_error_codes_matching="ConflictException|ValidationException",
            ),
            on_update=cr.AwsSdkCall(
                service="@aws-sdk/client-agent-registry-control",
                action="SubmitRegistryRecordForApproval",
                parameters={"registryId": registry_id, "recordId": record_id},
                physical_resource_id=cr.PhysicalResourceId.of(physical_id),
                ignore_error_codes_matching="ConflictException|ValidationException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["agent-registry:SubmitRegistryRecordForApproval"],
                        resources=[
                            f"arn:aws:agent-registry:{stack.region}:{stack.account}:registry/*"
                        ],
                    )
                ]
            ),
        )
        submit.node.add_dependency(r)
        return r

    for server, spec in inventory["servers"].items():
        record(
            f"RegistryMcp{server.title()}",
            f"argus-mcp-{server}",
            f"Argus {server} tools",
            spec["description"],
            "MCP",
            {
                "McpServer": {
                    "Data": json.dumps(
                        registry_server_descriptor(inventory, server, gateway_url)
                    ),
                    "DataSchemaVersion": "2025-12-11",
                    "AdditionalData": {
                        "Tools": {
                            "Data": json.dumps(
                                registry_tools_descriptor(inventory, server)
                            ),
                            "DataSchemaVersion": "2025-11-25",
                        }
                    },
                }
            },
        )
    for name, card in agent_cards.items():
        record(
            f"RegistryAgent{name.title()}",
            f"argus-agent-{name}",
            card["name"],
            card["description"],
            "AGENT",
            {"A2aAgentCard": {"Data": json.dumps(card), "DataSchemaVersion": "0.3"}},
        )
    for name, data in custom_records.items():
        record(
            f"RegistryCustom{name.title()}",
            f"argus-{name}",
            data.get("name", name),
            data.get("description", ""),
            "CUSTOM",  # the record type selects the descriptor format: Custom needs CUSTOM
            {"Custom": {"Data": json.dumps(data)}},
        )
    return registry
