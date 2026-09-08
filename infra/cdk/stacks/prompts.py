"""Bedrock Prompt Management for the agents' prompts (ADR-0012).

Each `agents/shared/prompts/<role>.md` becomes a managed prompt with an immutable version.
The version resource's description carries the file's content hash, so a text change
replaces the version (new number) and old versions stay for audit. Runtimes read the
version by ARN at start-up and record it in the provenance manifest; the repository file
stays the source of truth and the local stack reads it directly."""

from __future__ import annotations

import hashlib

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock

from .prompt_files import placeholders, prompt_files


def build_prompts(stack: Stack, root: str) -> dict[str, dict[str, str]]:
    """Managed prompt + version per role. Returns {role: {"arn", "version", "hash"}}."""
    out: dict[str, dict[str, str]] = {}
    for role, path in prompt_files(root).items():
        text = path.read_text()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        variables = placeholders(text)
        prompt = bedrock.CfnPrompt(
            stack,
            f"Prompt{role.title()}",
            name=f"argus-{role}",
            description=f"Argus {role} system prompt (git file agents/shared/prompts/{role}.md)",
            default_variant="default",
            variants=[
                bedrock.CfnPrompt.PromptVariantProperty(
                    name="default",
                    template_type="TEXT",
                    template_configuration=bedrock.CfnPrompt.PromptTemplateConfigurationProperty(
                        text=bedrock.CfnPrompt.TextPromptTemplateConfigurationProperty(
                            text=text,
                            input_variables=[
                                bedrock.CfnPrompt.PromptInputVariableProperty(name=v)
                                for v in variables
                            ]
                            or None,
                        )
                    ),
                    metadata=[
                        bedrock.CfnPrompt.PromptMetadataEntryProperty(
                            key="sha256_16", value=digest
                        ),
                        bedrock.CfnPrompt.PromptMetadataEntryProperty(
                            key="role", value=role
                        ),
                    ],
                )
            ],
        )
        version = bedrock.CfnPromptVersion(
            stack,
            f"PromptVersion{role.title()}",
            prompt_arn=prompt.attr_arn,
            description=f"argus-{role} {digest}",
        )
        version.apply_removal_policy(RemovalPolicy.RETAIN)
        out[role] = {
            "arn": prompt.attr_arn,
            "version": version.attr_version,
            "hash": digest,
        }
    return out
