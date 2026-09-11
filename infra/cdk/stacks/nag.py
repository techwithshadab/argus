"""cdk-nag (AwsSolutionsChecks, app.py) suppressions with the evidence for each.

What the stacks could fix, they fix (TLS on the topic, access logs on the balancers and
the archive, IAM database authentication, hosted rotation of the database password).
Every entry here names a wildcard or setting that is right as it is and why. New
findings fail synth until they are fixed or added here with a reason."""

from __future__ import annotations

from aws_cdk import Stack
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo

LAMBDA_BASIC = "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def _s(rule: str, reason: str, applies_to: list | None = None) -> NagPackSuppression:
    return NagPackSuppression(id=rule, reason=reason, applies_to=applies_to)


def _rx(pattern: str) -> RegexAppliesTo:
    return RegexAppliesTo(regex=f"/{pattern}/g")


def _path(stack: Stack, path: str, rules: list[NagPackSuppression]) -> None:
    """Suppress on one construct path, only when that construct exists in this synth
    (feed secrets exist only in live or keyed mode; a suppression naming a missing path
    fails the synth, and with it `cdk destroy`)."""
    if stack.node.try_find_child(path.split("/")[0]) is None:
        return
    NagSuppressions.add_resource_suppressions_by_path(
        stack, f"/{stack.stack_name}/{path}", rules
    )


def network(stack: Stack) -> None:
    _path(
        stack,
        "Deployer/Resource",
        [
            _s(
                "AwsSolutions-IAM4",
                "The deploy role: CDK deploys four stacks that create IAM roles, VPC, "
                "AgentCore and Bedrock resources, which needs administrator rights; it "
                "replaces root and is assumable only with MFA or by one named principal.",
                applies_to=[
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/AdministratorAccess"
                ],
            )
        ],
    )
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            _s(
                "CdkNagValidationFailure",
                "Security group rules reference the VPC CIDR token, which the rule cannot "
                "resolve; ingress is the VPC CIDR and the services group only "
                "(network_stack.py).",
                applies_to=["AwsSolutions-EC23"],
            ),
        ],
    )


def data(stack: Stack) -> None:
    _path(
        stack,
        "Aurora/Resource",
        [
            _s(
                "AwsSolutions-RDS10",
                "Deletion protection follows -c retainData, which defaults to true: "
                "`scripts/destroy.sh` passes retainData=false explicitly and "
                "snapshots the cluster first (docs/GAPS.md I1).",
            )
        ],
    )
    _path(
        stack,
        "AccessLogs/Resource",
        [
            _s(
                "AwsSolutions-S1",
                "This is the access-log bucket of the load balancers and the archive; "
                "logging it into itself would loop.",
            )
        ],
    )
    _path(
        stack,
        "DataKeySecret/Resource",
        [
            _s(
                "AwsSolutions-SMG4",
                "The pgp data key for personal-data columns: rotating it rewrites every "
                "encrypted row in one transaction, so it is an operator-run re-key "
                "(`make rekey-aws`, data/sql/009) rather than a scheduled rotation. The "
                "secret is KMS-encrypted and readable by three roles.",
            )
        ],
    )
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            _s(
                "AwsSolutions-IAM4",
                "Secrets Manager's hosted rotation function (a Serverless Application "
                "Repository app) attaches the basic Lambda execution policy: logs only.",
                applies_to=[LAMBDA_BASIC],
            ),
            _s(
                "AwsSolutions-IAM5",
                "The hosted rotation function's own policy (SAR app): ec2 network "
                "interfaces for the VPC and the secret it rotates.",
            ),
            _s(
                "AwsSolutions-L1",
                "The hosted rotation function's runtime is chosen by the SAR app.",
            ),
        ],
    )

    NagSuppressions.add_stack_suppressions(
        stack,
        [
            _s(
                "AwsSolutions-IAM4",
                "AWS Backup's own service role policy, attached by the BackupPlan "
                "construct. It grants the backup service the read and snapshot rights "
                "it needs on the resources the plan selects, and there is no "
                "customer-managed equivalent (I2).",
                applies_to=[
                    _rx(
                        r"^Policy::arn:.*iam::aws:policy/service-role/AWSBackupServiceRolePolicy.*$"
                    )
                ],
            )
        ],
    )


def platform(stack: Stack, *, grafana: bool) -> None:
    _path(
        stack,
        "PublicAlb/SecurityGroup/Resource",
        [
            _s(
                "AwsSolutions-EC23",
                "The watch floor is reachable from -c uiAllowedCidr (default open) "
                "behind AWS WAF and a Cognito sign-in on every listener (ADR-0018).",
            )
        ],
    )
    _path(
        stack,
        "InternalConsolesSg/Resource",
        [
            _s(
                "CdkNagValidationFailure",
                "Ingress is the VPC CIDR token (collector to the consoles).",
                applies_to=["AwsSolutions-EC23"],
            )
        ],
    )
    _path(
        stack,
        "SignIn/Pool/Resource",
        [
            _s(
                "AwsSolutions-COG8",
                "Lite feature plan by design: the Plus plan (threat protection) is a "
                "per-user charge; officers are few and created by an operator.",
            ),
            _s(
                "AwsSolutions-COG2",
                "MFA is optional by default so enabling it cannot lock out an officer who "
                "has not yet enrolled an authenticator; -c officerMfa=required "
                "enforces TOTP enrolment at the next sign-in (I10).",
            ),
        ],
    )
    _path(
        stack,
        "SweepScheduleDlq/Resource",
        [
            _s(
                "AwsSolutions-SQS3",
                "This queue is the dead-letter queue of the EventBridge Scheduler target.",
            )
        ],
    )
    for secret, why in (
        ("AisStreamKey", "an AISStream-issued API key"),
        ("OpenSanctionsKey", "an OpenSanctions-issued API key"),
    ):
        _path(
            stack,
            f"{secret}/Resource",
            [
                _s(
                    "AwsSolutions-SMG4",
                    f"Holds {why}; only the provider can issue a new one. deploy.sh "
                    "stores whatever `.env` carries and restarts the reader.",
                )
            ],
        )
    if grafana:
        _path(
            stack,
            "GrafanaAdminPassword/Resource",
            [
                _s(
                    "AwsSolutions-SMG4",
                    "Grafana applies the admin password from this secret on every start "
                    "(Dockerfile.grafana); rotating it needs a task restart, which a "
                    "rotation function cannot trigger. Redeploying regenerates it.",
                )
            ],
        )
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            _s(
                "AwsSolutions-IAM4",
                "CDK's custom-resource providers (AwsCustomResource, the Provider "
                "framework for the certificate issuer) attach the basic Lambda execution "
                "policy: CloudWatch Logs only.",
                applies_to=[LAMBDA_BASIC],
            ),
            _s(
                "AwsSolutions-ECS2",
                "Container environment carries configuration (hosts, modes, ARNs); every "
                "secret is a Secrets Manager reference in `secrets`, never an env value.",
            ),
            _s(
                "AwsSolutions-IAM5",
                "Actions with no resource-level permissions: ecr:GetAuthorizationToken "
                "(execution roles), cloudwatch:PutMetricData (API), the agent-registry "
                "Search/List/BatchGet family (workers), Grafana's CloudWatch data source "
                "reads (observability task), the X-Ray account-settings custom resources, "
                "and acm:ImportCertificate whose ARN exists only after the call.",
                applies_to=["Resource::*"],
            ),
            _s(
                "AwsSolutions-IAM5",
                "A log group prefix is the narrowest form CloudWatch Logs offers, and "
                "these are narrower than the `Resource::*` they replaced: Grafana may "
                "start a query only against this deployment's own groups, and the "
                "retention custom resources touch only the groups AgentCore creates "
                "(I11). The `:*` variants are the same groups' log streams.",
                applies_to=[
                    _rx(r"^Resource::arn:aws:logs:.*:log-group:/argus/\*:?\*?$"),
                    _rx(
                        r"^Resource::arn:aws:logs:.*:log-group:/aws/bedrock-agentcore/\*:?\*?$"
                    ),
                ],
            ),
            _s(
                "AwsSolutions-IAM5",
                "CDK's grant helpers for the queues' KMS key.",
                applies_to=["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
            ),
            _s(
                "AwsSolutions-IAM5",
                "Parameters under /argus/ and the AgentCore runtimes and gateways are "
                "created by the agents stack, which deploys after this one; a fixed ARN "
                "would be a dependency cycle (SSM breaks it, ADR-0013).",
                applies_to=[
                    _rx(r"^Resource::arn:aws:ssm:.*:parameter/argus/\*$"),
                    _rx(
                        r"^Resource::arn:aws:bedrock-agentcore:.*:(gateway|runtime)/\*$"
                    ),
                ],
            ),
            _s(
                "AwsSolutions-IAM5",
                "Object-level access to the positions archive (positions/day=... keys).",
                applies_to=[_rx(r"^Resource::<PositionsArchive.*\.Arn>/\*$")],
            ),
            _s(
                "AwsSolutions-IAM5",
                "CDK-generated: the scheduled task's events role runs tasks in this "
                "cluster; the Provider framework invokes the handler's versions.",
                applies_to=[
                    _rx(r"^Resource::arn:<AWS::Partition>:ecs:.*:task/.*/\*$"),
                    _rx(r"^Resource::<Certificates.*\.Arn>:\*$"),
                ],
            ),
        ],
    )


def agents(stack: Stack) -> None:
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            _s(
                "AwsSolutions-IAM4",
                "CDK's AwsCustomResource provider attaches the basic Lambda execution "
                "policy: CloudWatch Logs only.",
                applies_to=[LAMBDA_BASIC],
            ),
            _s(
                "AwsSolutions-IAM5",
                "The documented AgentCore runtime role: VPC network interfaces, "
                "ecr:GetAuthorizationToken, cloudwatch:PutMetricData and X-Ray put and "
                "sampling calls have no resource-level permissions. Also the log "
                "retention custom resources (groups AgentCore creates itself), the "
                "evaluations' log queries and the harness's workload tokens.",
                applies_to=["Resource::*"],
            ),
            _s(
                "AwsSolutions-IAM5",
                "Bedrock model allowlist by vendor (ADR-0002, lifecycle.py): cross-region "
                "inference profiles fan out to the vendor's models in other regions, so "
                "the resource is the vendor prefix; the guardrail version ARN changes "
                "with every policy edit (CfnGuardrailVersion is replaced).",
                applies_to=[
                    _rx(r"^Resource::arn:aws:bedrock:.*:foundation-model/.*$"),
                    _rx(r"^Resource::arn:aws:bedrock:.*:inference-profile/\*$"),
                    _rx(r"^Resource::arn:aws:bedrock:.*:guardrail/\*$"),
                ],
            ),
            _s(
                "AwsSolutions-IAM5",
                "Names AgentCore chooses: runtime log groups, workload identities with a "
                "random suffix, runtime endpoints, bundle versions, harness sessions, "
                "gateway targets, registry record ids, the harness image repositories, "
                "and the evaluations' log streams.",
                applies_to=[
                    _rx(
                        r"^Resource::arn:aws:logs:.*:log-group:/aws/bedrock-agentcore/.*$"
                    ),
                    _rx(r"^Resource::arn:aws:logs:.*:log-group:aws/spans:\*$"),
                    _rx(
                        r"^Resource::arn:aws:bedrock-agentcore:.*:workload-identity-directory/.*$"
                    ),
                    _rx(r"^Resource::<.*AgentRuntimeArn>.*$"),
                    _rx(
                        r"^Resource::arn:aws:bedrock-agentcore:.*:(gateway|policy-engine|configuration-bundle)/\*$"
                    ),
                    _rx(r"^Resource::<TaskingHarness\.Arn>/\*$"),
                    _rx(r"^Resource::<AgentsGateway\.GatewayArn>/\*$"),
                    _rx(r"^Resource::arn:aws:agent-registry:.*:registry/\*$"),
                    _rx(r"^Resource::arn:aws:ecr:.*:repository/harness-\*$"),
                ],
            ),
            _s(
                "AwsSolutions-IAM5",
                "Runtimes pull the CDK asset repository (bootstrap-qualified name); "
                "prompts are read by version (ARN:version).",
                applies_to=[
                    _rx(r"^Resource::arn:aws:ecr:.*:repository/\*$"),
                    _rx(r"^Resource::<Prompt.*\.Arn>:\*$"),
                ],
            ),
        ],
    )
