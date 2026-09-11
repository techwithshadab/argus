"""ECS Fargate platform: MCP servers, API, UI, AIS replay and an OTel collector.

Service Connect gives every task the same DNS names as docker compose (mcp-ais:8000, api:8000),
so container images and env vars are identical locally and on AWS. An internal ALB exposes the
MCP servers and API to the AgentCore runtimes, which live outside the ECS namespace."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_efs as efs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from .alarms import PlatformAlarms
from .assets import context_excludes
from .data_stack import DataStack
from .edge import (
    CertificateIssuer,
    OfficerSignIn,
    allow_oidc_egress,
    bearer_bypass,
    public_web_acl,
)
from .lifecycle import is_paused

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def collector_config(region: str, alb: str, grafana_stack: bool) -> str:
    """Collector config for the platform's own telemetry: traces to Tempo, metrics to
    Prometheus (remote write) and logs to Loki through the internal load balancer when the
    self-hosted observability task is deployed, otherwise to the debug exporter. Nothing
    goes to X-Ray or CloudWatch from here: those hold AgentCore's unified telemetry only
    (ADR-0012), so the collector task needs no AWS permissions."""
    traces = (["otlphttp/tempo"] if grafana_stack else []) or ["debug"]
    # CloudWatch is AgentCore's own telemetry store (runtime logs, unified traces,
    # evaluations, policy decisions) and nothing else: the platform's traces, metrics and
    # logs go to Tempo, Prometheus and Loki only.
    metrics = ["prometheusremotewrite"] if grafana_stack else ["debug"]
    logs = ["otlphttp/loki"] if grafana_stack else ["debug"]
    return json.dumps(
        {
            # The collector image is the upstream contrib build (same as the local stack), so
            # every exporter used here is available.
            "extensions": {},
            "receivers": {
                "otlp": {
                    "protocols": {
                        "grpc": {"endpoint": "0.0.0.0:4317"},
                        "http": {"endpoint": "0.0.0.0:4318"},
                    }
                },
                # Service-level indicators computed by the API (phase 5), scraped through the
                # internal ALB: Service Connect names only resolve for tasks started after the
                # target service exists, so a name would race the API at first deploy.
                "prometheus": {
                    "config": {
                        "scrape_configs": [
                            {
                                "job_name": "argus-api",
                                "scrape_interval": "60s",
                                # The listener carries the deploy's self-signed certificate.
                                "scheme": "https",
                                "tls_config": {"insecure_skip_verify": True},
                                "static_configs": [{"targets": [f"{alb}:8000"]}],
                            }
                        ]
                    }
                },
            },
            "processors": {
                "batch": {"timeout": "2s"},
                # AgentCore stamps its runtime session id on every span; ours are
                # `rt-<investigation id>-<role>` (one per invocation, parallel branches must
                # not share one), so fold them back to the investigation id (one session per investigation).
                "transform/session": {
                    "error_mode": "ignore",
                    "trace_statements": [
                        {
                            "context": "span",
                            "statements": [
                                'replace_pattern(attributes["session.id"], "^rt-([0-9a-f-]{36})-.*$", "$$1")',
                            ],
                        }
                    ],
                },
                # Load-balancer and Service Connect health checks would dominate every trace
                # store; drop those spans before any exporter sees them.
                "filter/health": {
                    "error_mode": "ignore",
                    "traces": {
                        "span": [
                            'IsMatch(name, "^(GET|HEAD) /(health|ping|api/health|api/public/health|-/ready|ready)$")',
                        ]
                    },
                },
            },
            "exporters": {
                "debug": {},
                # Consoles sit behind the internal ALB (ports below), whose name is known at synth,
                # so the collector needs no start-order dependency on them.
                "otlphttp/tempo": {"endpoint": f"http://{alb}:4319"},
                "otlphttp/loki": {"endpoint": f"http://{alb}:3100/otlp"},
                "prometheusremotewrite": {
                    "endpoint": f"http://{alb}:9090/api/v1/write",
                    "resource_to_telemetry_conversion": {"enabled": True},
                },
            },
            "service": {
                "extensions": [],
                "pipelines": {
                    "traces": {
                        "receivers": ["otlp"],
                        "processors": ["filter/health", "transform/session", "batch"],
                        "exporters": traces,
                    },
                    "metrics": {
                        "receivers": ["otlp", "prometheus"],
                        "processors": ["batch"],
                        "exporters": metrics,
                    },
                    "logs": {
                        "receivers": ["otlp"],
                        "processors": ["batch"],
                        "exporters": logs,
                    },
                },
            },
        }
    )


class PlatformStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        vpc: ec2.Vpc,
        services_sg: ec2.SecurityGroup,
        agents_sg: ec2.SecurityGroup,
        nat_gateway_ids: list[str],
        data: DataStack,
        **kw,
    ):
        super().__init__(scope, cid, **kw)
        # Immutable import: CDK will not try to add ingress rules to the network stack's group.
        services_sg = ec2.SecurityGroup.from_security_group_id(
            self, "ServicesSgImport", services_sg.security_group_id, mutable=False
        )
        scenario_end = (
            self.node.try_get_context("scenarioEnd") or "2026-09-01T08:00:00Z"
        )
        paused = is_paused(self)
        desired = 0 if paused else 1
        # Who may reach the UI. Default is open; set -c uiAllowedCidr=<office or VPN CIDR>.
        ui_cidr = self.node.try_get_context("uiAllowedCidr") or "0.0.0.0/0"
        ais_mode = str(self.node.try_get_context("aisMode") or "replay").lower()
        # Live mode: catalogue areas (data/areas.yaml) watched besides the scenario's box.
        watch_areas = str(self.node.try_get_context("watchAreas") or "all")
        # Self-hosted Grafana, Tempo, Loki and Prometheus as one Fargate task (default on).
        grafana_stack = self.grafana_stack = (
            str(self.node.try_get_context("grafanaStack") or "true").lower() != "false"
        )
        open_sanctions = (
            str(self.node.try_get_context("openSanctions") or "").lower() == "true"
        )
        cert_arn = self.node.try_get_context("uiCertificateArn") or ""
        # With a domain certificate, officers use that (lowercase) domain; without one the
        # balancer's own name, lowercased by the certificate issuer (Cognito requirement).
        ui_domain = str(self.node.try_get_context("uiDomain") or "").lower()
        # First officer account (Cognito emails a temporary password) and who may assume
        # the operator role (default: any principal in this account).
        officer_email = self.node.try_get_context("officerEmail") or ""
        operator_principal = self.node.try_get_context("operatorPrincipalArn") or ""
        # Indexing every span costs money for a signal a sample already gives (I13).
        trace_sampling = max(
            1, min(100, int(self.node.try_get_context("traceSampling") or 10))
        )
        monthly_budget = int(self.node.try_get_context("monthlyBudgetUsd") or 500)
        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENHANCED,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name="argus.local", use_for_service_connect=True
            ),
        )
        self.cluster = cluster
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name="/argus/services",
            # A month, not three days. Every platform service logs here, the runbook's
            # own troubleshooting steps say "check the task's logs", and a rolled-back
            # deployment or a stalled feed may not be noticed over a long weekend (I17).
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # ---- X-Ray Transaction Search, on by default (`-c transactionSearch=false`) ----
        # AgentCore's unified telemetry is indexed and shown (CloudWatch GenAI Observability,
        # AgentCore Evaluations) through Transaction Search, so it is on unless switched off.
        # Switching the account trace destination pulls in Application Signals discovery,
        # a CloudTrail service-linked channel and the aws/spans log group (ADR-0012).
        if (
            str(self.node.try_get_context("transactionSearch") or "true").lower()
            != "false"
        ):
            # ---- X-Ray account settings, as resources so deploy applies and destroy reverts them ----
            # Traces from AgentCore and the ADOT collector land in CloudWatch Transaction Search.
            # Transaction Search creates and configures the aws/spans log group when the destination is
            # switched, so the custom resource needs those log permissions as well as the X-Ray ones.
            xray_policy = cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "xray:UpdateTraceSegmentDestination",
                            "xray:GetTraceSegmentDestination",
                            "xray:UpdateIndexingRule",
                            "xray:GetIndexingRules",
                            "logs:CreateLogGroup",
                            "logs:DescribeLogGroups",
                            "logs:PutRetentionPolicy",
                            "logs:PutResourcePolicy",
                            "logs:DescribeResourcePolicies",
                            "application-signals:StartDiscovery",
                            "iam:CreateServiceLinkedRole",
                            "cloudtrail:CreateServiceLinkedChannel",
                        ],
                        resources=["*"],
                    )
                ]
            )
            # X-Ray writes Transaction Search spans into CloudWatch Logs itself, so the
            # account's Logs resource policy must let it before the destination switches
            # ("XRay does not have permission to call PutLogEvents on the aws/spans Log
            # Group" otherwise, and the whole stack rolls back).
            spans_policy = logs.CfnResourcePolicy(
                self,
                "TransactionSearchLogsPolicy",
                policy_name="TransactionSearchAccess",
                policy_document=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "TransactionSearchXRayAccess",
                                "Effect": "Allow",
                                "Principal": {"Service": "xray.amazonaws.com"},
                                "Action": "logs:PutLogEvents",
                                "Resource": [
                                    f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*",
                                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/application-signals/data:*",
                                ],
                                "Condition": {
                                    "ArnLike": {
                                        "aws:SourceArn": f"arn:aws:xray:{self.region}:{self.account}:*"
                                    },
                                    "StringEquals": {"aws:SourceAccount": self.account},
                                },
                            }
                        ],
                    }
                ),
            )
            destination = cr.AwsCustomResource(
                self,
                "XrayTraceDestination",
                on_create=cr.AwsSdkCall(
                    service="XRay",
                    action="updateTraceSegmentDestination",
                    parameters={"Destination": "CloudWatchLogs"},
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "argus-xray-destination"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="XRay",
                    action="updateTraceSegmentDestination",
                    parameters={"Destination": "CloudWatchLogs"},
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "argus-xray-destination"
                    ),
                ),
                on_delete=cr.AwsSdkCall(
                    service="XRay",
                    action="updateTraceSegmentDestination",
                    parameters={"Destination": "XRay"},
                    ignore_error_codes_matching=".*",  # "already set to XRay" must not block a rollback
                ),
                policy=xray_policy,
                log_group=log_group,
            )
            destination.node.add_dependency(spans_policy)
            cr.AwsCustomResource(
                self,
                "XrayIndexingRule",
                on_create=cr.AwsSdkCall(
                    service="XRay",
                    action="updateIndexingRule",
                    parameters={
                        "Name": "Default",
                        "Rule": {
                            "Probabilistic": {
                                "DesiredSamplingPercentage": trace_sampling
                            }
                        },
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "argus-xray-indexing"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="XRay",
                    action="updateIndexingRule",
                    parameters={
                        "Name": "Default",
                        "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 100}},
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "argus-xray-indexing"
                    ),
                ),
                on_delete=cr.AwsSdkCall(
                    service="XRay",
                    action="updateIndexingRule",
                    parameters={
                        "Name": "Default",
                        "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 5}},
                    },
                    ignore_error_codes_matching=".*",
                ),
                policy=xray_policy,
                log_group=log_group,
            )

        # ---- images (x86 for Fargate; the agents stack builds arm64 for AgentCore) ----
        # What each console image copies (see services/observability/Dockerfile.*).
        OBS_CONTEXT = {
            "grafana": ["observability/aws"],
            "prometheus": [
                "observability/aws/prometheus.yml",
                "observability/alerts.yml",
            ],
            "tempo": ["observability/aws/tempo.yaml"],
            "loki": ["observability/aws/loki.yaml"],
        }

        def img(
            name: str, dockerfile: str, keep: list[str]
        ) -> ecr_assets.DockerImageAsset:
            # `keep` is exactly what the Dockerfile copies: the asset hash then moves only
            # when those paths change, so a deploy leaves untouched services alone.
            return ecr_assets.DockerImageAsset(
                self,
                f"Img{name}",
                directory=REPO_ROOT,
                file=dockerfile,
                platform=ecr_assets.Platform.LINUX_AMD64,
                exclude=context_excludes(REPO_ROOT, [dockerfile, *keep]),
            )

        api_image = img("Api", "services/api/Dockerfile", ["services/api"])
        ui_image = img("Ui", "services/ui/Dockerfile", ["services/ui"])
        replay_image = img(
            "Replay", "services/ais-replay/Dockerfile", ["services/ais-replay", "data"]
        )

        # ---- shared env ----
        db_env = {
            "PGHOST": data.cluster.cluster_endpoint.hostname,
            "PGPORT": "5432",
            "PGDATABASE": "argus",
            "PGUSER": "argus",
            # Re-read after a rotation (dbconn.py): the injected PGPASSWORD is a snapshot.
            "PGPASSWORD_SECRET_ARN": data.db_secret.secret_arn,
        }
        db_secret = {
            "PGPASSWORD": ecs.Secret.from_secrets_manager(data.db_secret, "password")
        }
        # Personal-data encryption key: read once at startup by every service that touches registry rows.
        data_key_env = {"DATA_KEY_SECRET_ARN": data.data_key_secret.secret_arn}
        otel_env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "DEPLOY_ENV": "aws",
            # Every clock in the system: the scenario's end in replay, the wall clock when live.
            "AIS_MODE": ais_mode,
        }

        # Keys for external feeds live in stack-owned secrets whose values are set out of band
        # (deploy.sh does it from the environment or .env), so a key never enters a template.
        def feed_secret(
            cid: str, description: str, json_key: str | None = None
        ) -> sm.Secret:
            # A JSON-shaped secret can also back an AgentCore Identity API-key credential
            # provider (which references a secret by JSON key); the placeholder value is
            # replaced by deploy.sh.
            template = (
                {
                    "generate_string_key": "unused",
                    "secret_string_template": json.dumps({json_key: "unset"}),
                }
                if json_key
                else {}
            )
            secret = sm.Secret(
                self,
                cid,
                description=description,
                removal_policy=RemovalPolicy.DESTROY,
                generate_secret_string=sm.SecretStringGenerator(**template)
                if json_key
                else None,
            )
            CfnOutput(self, f"{cid}Arn", value=secret.secret_arn)
            return secret

        # The tool runtimes (agents stack) read these by ARN at start-up.
        self.opensanctions_secret = (
            feed_secret(
                "OpenSanctionsKey",
                "OpenSanctions API key for sanctions screening",
                json_key="api_key",
            )
            if open_sanctions
            else None
        )
        self.open_sanctions = open_sanctions
        self.ais_mode = ais_mode
        redis_url = f"rediss://{data.cache_endpoint}:{data.cache_port}/0"
        # Agent-only API routes verify the caller's IAM role through STS (ADR-0001); tool
        # access is decided by the AgentCore Gateway's policy engine (ADR-0011).
        auth_env = {"TOOL_AUTH": "aws-iam", "TOOL_ALLOWED_ACCOUNT": self.account}
        # Operator tooling (evals, demo scripts) calls the API as this role with a caller
        # token: `aws sts assume-role`, then EVAL_AUTH=aws-iam (docs/RUNBOOK.md).
        # This role may review findings and approve collection requests (ADR-0019), so
        # its trust matters as much as an officer's sign-in. A named principal is the
        # right answer; without one the fallback is the account with MFA, matching the
        # deployer role. Account-wide trust with no second factor let any principal in
        # the account act as a watch officer (I7).
        operator_role = iam.Role(
            self,
            "Operator",
            role_name="argus-operator",
            assumed_by=(
                iam.ArnPrincipal(operator_principal)
                if operator_principal
                else iam.AccountPrincipal(self.account).with_conditions(
                    {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
                )
            ),
            max_session_duration=Duration.hours(4),
            description="Calls the Argus API with a caller token (evals, scripts)",
        )
        CfnOutput(self, "OperatorRoleArn", value=operator_role.role_arn)

        # ---- CI assumes the operator role through GitHub's OIDC provider, not a key
        # (I7, I14). The evals workflow held a long-lived access key pair in repository
        # secrets: a credential that cannot be rotated by the stack, does not expire, and
        # is enough to review findings. With `-c githubRepo=owner/name` the workflow gets
        # a short-lived session instead, scoped to this repository's own workflows.
        github_repo = str(self.node.try_get_context("githubRepo") or "").strip()
        if github_repo:
            provider = iam.OpenIdConnectProvider(
                self,
                "GithubOidc",
                url="https://token.actions.githubusercontent.com",
                client_ids=["sts.amazonaws.com"],
            )
            ci_role = iam.Role(
                self,
                "CiOperator",
                role_name="argus-ci-operator",
                assumed_by=iam.WebIdentityPrincipal(
                    provider.open_id_connect_provider_arn,
                    {
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        # Only workflows in this repository, not any repository that
                        # happens to use the same provider.
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": f"repo:{github_repo}:*"
                        },
                    },
                ),
                max_session_duration=Duration.hours(1),
                description="GitHub Actions assumes this to run the eval gate",
            )
            ci_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["sts:AssumeRole"], resources=[operator_role.role_arn]
                )
            )
            operator_role.assume_role_policy.add_statements(
                iam.PolicyStatement(
                    actions=["sts:AssumeRole"], principals=[ci_role.grant_principal]
                )
            )
            CfnOutput(self, "CiRoleArn", value=ci_role.role_arn)

        # ---- durable jobs (phase 3, ADR-0016): one SQS queue per job kind, each with a
        # dead-letter queue. Sweeps and investigations have different durations and a
        # burst of investigations must never delay a timed sweep, so they get separate
        # queues, workers and visibility timeouts (job timeout + 60 s margin, matching
        # services/api/jobqueue.py::visibility_for; the worker heartbeats while it runs).
        dlqs: dict[str, sqs.Queue] = {}

        def job_queue(cid: str, name: str, visibility_s: int) -> sqs.Queue:
            dlq = dlqs[name.removeprefix("argus-")] = sqs.Queue(
                self,
                f"{cid}Dlq",
                queue_name=f"{name}-dlq",
                retention_period=Duration.days(14),
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=data.kms_key,
                enforce_ssl=True,
            )
            return sqs.Queue(
                self,
                cid,
                queue_name=name,
                visibility_timeout=Duration.seconds(visibility_s),
                retention_period=Duration.days(4),
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=data.kms_key,
                enforce_ssl=True,
                dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=4, queue=dlq),
            )

        jobs_queue = job_queue("Jobs", "argus-jobs", 1200 + 60)  # investigations
        sweeps_queue = job_queue("Sweeps", "argus-sweeps", 600 + 60)
        self.jobs_queue, self.sweeps_queue = jobs_queue, sweeps_queue
        job_env = {
            "JOB_BACKEND": "sqs",
            "JOB_QUEUE_URL": jobs_queue.queue_url,
            "JOB_SWEEP_QUEUE_URL": sweeps_queue.queue_url,
        }
        auto_investigate = (
            self.node.try_get_context("autoInvestigateSeverities") or "high"
        )
        sweep_minutes = int(self.node.try_get_context("sweepIntervalMinutes") or 30)

        # ---- internal ALB (for AgentCore runtimes) ----
        internal_alb = elbv2.ApplicationLoadBalancer(
            self,
            "InternalAlb",
            vpc=vpc,
            internet_facing=False,
            security_group=services_sg,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )
        self.internal_alb_dns = internal_alb.load_balancer_dns_name
        internal_alb.log_access_logs(data.logs_bucket, "internal-alb")
        # TLS on the API listener (A7): a deploy-time certificate for the balancer's own
        # name; agents read its public half from SSM (INTERNAL_CA_SSM) to trust it.
        issuer = CertificateIssuer(self, "Certificates")
        internal_cert = issuer.issue("InternalCert", self.internal_alb_dns)
        ssm.StringParameter(
            self,
            "InternalCa",
            parameter_name="/argus/internal-ca",
            string_value=internal_cert.certificate_pem,
            description="Public certificate of the Argus internal load balancer",
        )
        internal_certs = [
            elbv2.ListenerCertificate.from_arn(internal_cert.certificate_arn)
        ]
        target_groups: dict[str, elbv2.ApplicationTargetGroup] = {}
        plain_services: dict[str, ecs.FargateService] = {}

        ui_sg = ec2.SecurityGroup(
            self, "UiSg", vpc=vpc, description="UI tasks (reached from the public ALB)"
        )

        def service(
            name: str,
            image: ecr_assets.DockerImageAsset,
            port: int,
            env: dict,
            secrets: dict | None = None,
            cpu=512,
            mem=1024,
            internal_port: int | None = None,
            health="/health",
            extra_sgs: list | None = None,
            certificates: list | None = None,
            replicas: int = 1,
        ):
            td = ecs.FargateTaskDefinition(
                self,
                f"Td{name}",
                cpu=cpu,
                memory_limit_mib=mem,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.X86_64
                ),
            )
            td.add_container(
                "app",
                image=ecs.ContainerImage.from_docker_image_asset(image),
                environment=env,
                secrets=secrets or {},
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=name, log_group=log_group
                ),
                port_mappings=[ecs.PortMapping(container_port=port, name="http")],
            )
            # Identity-side grant only: a resource policy on the secret would make the data stack depend
            # on this stack (a cycle). Same for the archive bucket and KMS key below.
            td.task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        data.data_key_secret.secret_arn,
                        data.db_secret.secret_arn,
                    ],
                )
            )
            td.task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt"], resources=[data.kms_key.key_arn]
                )
            )
            svc = ecs.FargateService(
                self,
                f"Svc{name}",
                cluster=cluster,
                task_definition=td,
                desired_count=0 if paused else replicas,
                security_groups=[services_sg, *(extra_sgs or [])],
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                service_connect_configuration=ecs.ServiceConnectProps(
                    services=[
                        ecs.ServiceConnectService(
                            port_mapping_name="http",
                            discovery_name=name,
                            dns_name=name,
                            port=port,
                        )
                    ]
                ),
                # A service that carries traffic keeps a healthy task through a deploy
                # and a rollback (I3). At 0% every deploy stopped the last task before
                # starting the new one, so the API, the UI and the collector went down
                # on every routine deploy and the `*-down` alarms paged for it. Single
                # -task services (the replay ingest, which must not double-write, and
                # the workers, which drain their own queue) keep the old behaviour.
                min_healthy_percent=100 if replicas > 1 else 0,
                max_healthy_percent=200,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            )
            # Service Connect only injects names of services that already existed when a task
            # started, and every service exports telemetry to otel-collector by name.
            svc.node.add_dependency(collector)
            if internal_port:
                listener = internal_alb.add_listener(
                    f"L{name}",
                    port=internal_port,
                    protocol=(
                        elbv2.ApplicationProtocol.HTTPS
                        if certificates
                        else elbv2.ApplicationProtocol.HTTP
                    ),
                    certificates=certificates,
                    ssl_policy=elbv2.SslPolicy.TLS13_RES if certificates else None,
                )
                target_groups[name] = listener.add_targets(
                    f"T{name}",
                    port=port,
                    protocol=elbv2.ApplicationProtocol.HTTP,
                    targets=[svc],
                    health_check=elbv2.HealthCheck(
                        path=health, interval=Duration.seconds(15)
                    ),
                )
            else:
                plain_services[name] = svc
            return td, svc

        # One notification path for CloudWatch alarms and Grafana alerts alike.
        topic = sns.Topic(
            self,
            "Alerts",
            topic_name="argus-alerts",
            display_name="Argus alerts",
            enforce_ssl=True,
        )
        # enforce_ssl installs a topic policy, which replaces SNS's default one; CloudWatch
        # Alarms publish as a service principal and need their own allow back (without it
        # every alarm action fails with "CloudWatch Alarms is not authorized to perform:
        # SNS:Publish"). Grafana's task role publishes through its identity policy.
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarms",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        email = self.node.try_get_context("alertEmail") or ""
        if email:
            topic.add_subscription(subs.EmailSubscription(email))

        # ---- cost (I13). Nothing in the stack noticed spend until the bill arrived, and
        # this deployment idles at roughly $400 a month with the consoles running. A
        # budget with forecast and actual notifications is the cheapest guard there is:
        # it costs nothing and it fires before the month ends, not after. Budgets are a
        # global (us-east-1) service and notify SNS directly, so the topic needs an allow
        # for the budgets principal. `-c monthlyBudgetUsd=0` switches it off.
        if monthly_budget > 0:
            topic.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="AllowBudgets",
                    principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                    actions=["sns:Publish"],
                    resources=[topic.topic_arn],
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                )
            )
            budgets.CfnBudget(
                self,
                "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name="argus-monthly",
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(
                        amount=monthly_budget, unit="USD"
                    ),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type=kind,
                            threshold=threshold,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(
                                address=topic.topic_arn, subscription_type="SNS"
                            )
                        ],
                    )
                    # Half the budget spent is a note; the whole of it forecast is a
                    # warning that arrives while the month can still be changed.
                    for kind, threshold in (
                        ("ACTUAL", 50),
                        ("ACTUAL", 90),
                        ("FORECASTED", 100),
                    )
                ],
            )

        # Consoles behind the public ALB (Grafana) need a group this stack may edit:
        # the services group is an immutable import, so rules added to it are dropped silently.
        consoles_sg = ec2.SecurityGroup(
            self,
            "ConsolesSg",
            vpc=vpc,
            description="Argus consoles reached through the public ALB",
            allow_all_outbound=True,
        )

        # ---- Self-hosted observability: Grafana, Tempo, Loki, Prometheus in one task ----
        # The same images and dashboards as the local compose stack, on Fargate with EFS for
        # state. Containers in one awsvpc task share localhost, so the configs point at
        # localhost; the collector reaches them by Service Connect name. Off with
        # `-c grafanaStack=false`; CloudWatch and X-Ray keep receiving everything regardless.
        observability = None
        obs_fs = None
        if grafana_stack:
            obs_fs = efs.FileSystem(
                self,
                "ObsFs",
                vpc=vpc,
                removal_policy=RemovalPolicy.DESTROY,
                encrypted=True,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            )
            obs_fs.connections.allow_default_port_from(
                services_sg, "observability task"
            )
            obs_td = ecs.FargateTaskDefinition(
                self,
                "TdObservability",
                cpu=1024,
                memory_limit_mib=3072,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.X86_64
                ),
            )
            obs_fs.grant_read_write(obs_td.task_role)
            # Grafana's CloudWatch datasource and its SNS contact point use the task role.
            # Split three ways on purpose (I11). Grafana could query every log group in
            # the account; only StartQuery can be scoped, because CloudWatch Logs
            # matches GetQueryResults and StopQuery by query id and DescribeLogGroups by
            # nothing at all. Scoping those would break every Logs panel at query time
            # rather than at deploy time.
            obs_td.task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "cloudwatch:GetMetricData",
                        "cloudwatch:ListMetrics",
                        "cloudwatch:GetMetricStatistics",
                        "cloudwatch:DescribeAlarms",
                        "logs:DescribeLogGroups",
                        "ec2:DescribeRegions",
                        "tag:GetResources",
                    ],
                    resources=["*"],
                )
            )
            obs_td.task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:StartQuery"],
                    resources=[
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/argus/*",
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/argus/*:*",
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*",
                        f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*:*",
                    ],
                )
            )
            obs_td.task_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:GetQueryResults", "logs:StopQuery"],
                    resources=["*"],  # matched by query id, not by log group
                )
            )
            topic.grant_publish(obs_td.task_role)
            grafana_password = sm.Secret(
                self,
                "GrafanaAdminPassword",
                description="Grafana admin password for the Argus observability task",
                generate_secret_string=sm.SecretStringGenerator(
                    exclude_punctuation=True, password_length=24
                ),
                removal_policy=RemovalPolicy.DESTROY,
            )
            CfnOutput(self, "GrafanaAdminSecretArn", value=grafana_password.secret_arn)

            def obs_container(
                name: str,
                dockerfile: str,
                port: int,
                mount: str | None,
                uid: int,
                environment: dict | None = None,
                secrets: dict | None = None,
            ) -> None:
                if mount:
                    ap = obs_fs.add_access_point(
                        f"Ap{name.title()}",
                        path=f"/{name}",
                        posix_user=efs.PosixUser(uid=str(uid), gid=str(uid)),
                        create_acl=efs.Acl(
                            owner_uid=str(uid), owner_gid=str(uid), permissions="750"
                        ),
                    )
                    obs_td.add_volume(
                        name=name,
                        efs_volume_configuration=ecs.EfsVolumeConfiguration(
                            file_system_id=obs_fs.file_system_id,
                            transit_encryption="ENABLED",
                            authorization_config=ecs.AuthorizationConfig(
                                access_point_id=ap.access_point_id, iam="ENABLED"
                            ),
                        ),
                    )
                c = obs_td.add_container(
                    name,
                    image=ecs.ContainerImage.from_docker_image_asset(
                        img(
                            f"Obs{name.title()}",
                            dockerfile,
                            OBS_CONTEXT[name],
                        )
                    ),
                    environment=environment or {},
                    secrets=secrets or {},
                    logging=ecs.LogDrivers.aws_logs(
                        stream_prefix=name, log_group=log_group
                    ),
                    port_mappings=[ecs.PortMapping(container_port=port, name=name)]
                    + (
                        [ecs.PortMapping(container_port=4318, name="tempo-http")]
                        if name == "tempo"
                        else []
                    ),
                )
                if mount:
                    c.add_mount_points(
                        ecs.MountPoint(
                            container_path=mount, source_volume=name, read_only=False
                        )
                    )

            # Grafana's state lives in a `grafana` database on Aurora (SQLite on EFS took
            # twenty minutes to migrate); the replay task creates the database at start.
            obs_container(
                "grafana",
                "services/observability/Dockerfile.grafana",
                3000,
                None,
                472,
                environment={
                    "AWS_REGION": self.region,
                    "ALERT_SNS_TOPIC_ARN": topic.topic_arn,
                    "GF_DATABASE_TYPE": "postgres",
                    "GF_DATABASE_HOST": f"{data.cluster.cluster_endpoint.hostname}:5432",
                    "GF_DATABASE_NAME": "grafana",
                    "GF_DATABASE_USER": "argus",
                    "GF_DATABASE_SSL_MODE": "require",
                },
                secrets={
                    "GF_SECURITY_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(
                        grafana_password
                    ),
                    "GF_DATABASE_PASSWORD": ecs.Secret.from_secrets_manager(
                        data.db_secret, "password"
                    ),
                },
            )
            # Prometheus keeps its TSDB on the task's ephemeral disk: NFS is unsupported for
            # it, and the SLIs also live in CloudWatch, so a restart losing history is fine.
            obs_container(
                "prometheus",
                "services/observability/Dockerfile.prometheus",
                9090,
                None,
                65534,
            )
            obs_container(
                "tempo",
                "services/observability/Dockerfile.tempo",
                4317,
                "/var/tempo",
                10001,
            )
            obs_container(
                "loki", "services/observability/Dockerfile.loki", 3100, "/loki", 10001
            )
            observability = ecs.FargateService(
                self,
                "SvcObservability",
                cluster=cluster,
                task_definition=obs_td,
                desired_count=desired,
                min_healthy_percent=0,
                max_healthy_percent=100,  # EFS-backed singletons: never two writers
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                health_check_grace_period=Duration.seconds(300),
                security_groups=[services_sg, consoles_sg],
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                service_connect_configuration=ecs.ServiceConnectProps(
                    services=[
                        ecs.ServiceConnectService(
                            port_mapping_name=n, discovery_name=n, dns_name=n, port=p
                        )
                        for n, p in (
                            ("grafana", 3000),
                            ("prometheus", 9090),
                            ("tempo", 4317),
                            ("loki", 3100),
                        )
                    ]
                ),
            )

        public_alb = elbv2.ApplicationLoadBalancer(
            self,
            "PublicAlb",
            vpc=vpc,
            internet_facing=True,
            drop_invalid_header_fields=True,
        )
        public_dns = public_alb.load_balancer_dns_name
        public_alb.log_access_logs(data.logs_bucket, "public-alb")
        if cert_arn:
            if not ui_domain:
                raise ValueError(
                    "uiCertificateArn needs uiDomain (the name officers use)"
                )
            watch_host = ui_domain
        else:
            # No domain: a self-signed certificate for the balancer's own name, generated at
            # deploy time (the private key never enters the template).
            public_cert = issuer.issue("PublicCert", public_dns)
            cert_arn, watch_host = (
                public_cert.certificate_arn,
                public_cert.dns_name_lower,
            )
        sign_in = OfficerSignIn(
            self,
            "SignIn",
            account=self.account,
            callback_urls=[
                f"https://{watch_host}/oauth2/idpresponse",
                f"https://{watch_host}:3000/oauth2/idpresponse",
            ],
            logout_urls=[f"https://{watch_host}/", f"https://{watch_host}:3000/"],
            initial_officer_email=officer_email,
            mfa_required=(
                str(self.node.try_get_context("officerMfa") or "").lower() == "required"
            ),
        )
        CfnOutput(self, "OfficerPoolId", value=sign_in.pool.user_pool_id)

        # ---- Consoles on the internal ALB, for the collector and the agents' telemetry ----
        # The internal ALB carries the immutable shared group (8000-8010, 4317-4318 only), so
        # the console listener ports need a group this stack owns.
        internal_console_sg = ec2.SecurityGroup(
            self,
            "InternalConsolesSg",
            vpc=vpc,
            description="Internal ALB listeners for the observability consoles",
            allow_all_outbound=True,
        )
        for port, what in (
            (4319, "OTLP/HTTP to Tempo"),
            (3100, "OTLP/HTTP logs to Loki"),
            (9090, "remote write to Prometheus"),
        ):
            internal_console_sg.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(port), what
            )
        internal_alb.add_security_group(internal_console_sg)

        def internal_target(
            name: str,
            listener_port: int,
            svc,
            container: str,
            container_port: int,
            health_path: str,
            health_port: str,
        ) -> None:
            lst = internal_alb.add_listener(
                f"L{name}", port=listener_port, protocol=elbv2.ApplicationProtocol.HTTP
            )
            lst.add_targets(
                f"T{name}",
                port=container_port,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    svc.load_balancer_target(
                        container_name=container, container_port=container_port
                    )
                ],
                health_check=elbv2.HealthCheck(
                    path=health_path, port=health_port, interval=Duration.seconds(15)
                ),
                deregistration_delay=Duration.seconds(10),
            )

        if observability is not None:
            # Tempo's readiness lives on its API port, not the OTLP port the ALB forwards to;
            # the automatic rule only opens the target port, so open the check port too.
            consoles_sg.add_ingress_rule(
                internal_alb.connections.security_groups[0],
                ec2.Port.tcp(3200),
                "internal ALB health check to Tempo",
            )
            internal_target(
                "tempo", 4319, observability, "tempo", 4318, "/ready", "3200"
            )
            internal_target("loki", 3100, observability, "loki", 3100, "/ready", "3100")
            internal_target(
                "prometheus",
                9090,
                observability,
                "prometheus",
                9090,
                "/-/ready",
                "9090",
            )

        # ---- OTel collector ----
        col_td = ecs.FargateTaskDefinition(
            self, "TdCollector", cpu=256, memory_limit_mib=512
        )
        col_td.add_container(
            "app",
            # Built as an asset, not pulled from Docker Hub at every task start: every
            # service depends on the collector by name, so a rate limit there stopped
            # the whole platform from starting (I18). The Dockerfile copies nothing, so
            # its keep list is empty and its hash never moves with unrelated edits.
            image=ecs.ContainerImage.from_docker_image_asset(
                img("Collector", "services/observability/Dockerfile.collector", [])
            ),
            command=["--config=env:OTEL_CONFIG"],
            environment={
                "OTEL_CONFIG": collector_config(
                    self.region, self.internal_alb_dns, grafana_stack
                )
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="otel-collector", log_group=log_group
            ),
            port_mappings=[
                ecs.PortMapping(container_port=4318, name="http"),
                ecs.PortMapping(container_port=4317, name="grpc"),
            ],
        )
        collector = ecs.FargateService(
            self,
            "SvcCollector",
            cluster=cluster,
            task_definition=col_td,
            # Every service exports telemetry here and depends on it by name, so a
            # collector that drops to zero loses spans from the whole platform for
            # the length of a deploy (I3).
            desired_count=0 if paused else 2,
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            security_groups=[services_sg],
            service_connect_configuration=ecs.ServiceConnectProps(
                services=[
                    ecs.ServiceConnectService(
                        port_mapping_name="http",
                        discovery_name="otel-collector",
                        dns_name="otel-collector",
                        port=4318,
                    )
                ]
            ),
        )
        plain_services["collector"] = collector

        # Collector on the internal ALB so AgentCore runtimes (outside the namespace) can also export here if desired.
        lcol = internal_alb.add_listener(
            "LCollector", port=4318, protocol=elbv2.ApplicationProtocol.HTTP
        )
        lcol.add_targets(
            "TCollector",
            port=4318,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[collector],
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200-499"),
        )

        # ---- API (invokes the orchestrator on AgentCore) ----
        api_td, api_svc = service(
            "api",
            api_image,
            8000,
            {
                **db_env,
                **otel_env,
                "REDIS_URL": redis_url,
                "ORCHESTRATOR_MODE": "agentcore",
                "SCENARIO_END": scenario_end,
                "AWS_REGION": self.region,
                **auth_env,
                **data_key_env,
                **job_env,
                "AUTO_INVESTIGATE_SEVERITIES": auto_investigate,
                # Two allowlists, not one (ADR-0019): the agent roles reach the
                # agent-only routes, and only the operator role may take an officer
                # action. A single list let the orchestrator approve its own tasking.
                "TOOL_ALLOWED_ROLES": (
                    "argus-agent-watch,argus-agent-orchestrator,argus-operator"
                ),
                "AGENT_ALLOWED_ROLES": "argus-agent-watch,argus-agent-orchestrator",
                "OFFICER_ALLOWED_ROLES": "argus-operator",
                # The public ALB signs officers in; the API verifies its id token.
                "OFFICER_AUTH": "oidc",
                "OIDC_SIGNER": public_alb.load_balancer_arn,
                "OIDC_ISSUER": sign_in.pool.user_pool_provider_url,
                # /health and /metrics answer 404 to this host and to it alone: the
                # collector scrapes the API through the *internal* balancer, so an
                # x-forwarded-for check would have blocked it too (it did).
                "PUBLIC_HOST": public_alb.load_balancer_dns_name,
                "GIT_SHA": os.getenv("GIT_SHA", "unknown"),
            },
            db_secret,
            internal_port=8000,
            certificates=internal_certs,
            replicas=2,
        )
        # The officer's verdict becomes a CloudWatch metric next to the evaluation scores.
        api_td.task_role.add_to_policy(
            iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"])
        )
        api_td.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/argus/*"
                ],
            )
        )
        jobs_queue.grant_send_messages(api_td.task_role)
        sweeps_queue.grant_send_messages(api_td.task_role)

        # ---- job workers: same image as the API, different command; one service per
        # queue so investigations (4 in flight) and sweeps (1, timed) never compete ----
        def worker_service(cid: str, queue_: sqs.Queue, kind: str, concurrency: int):
            td = ecs.FargateTaskDefinition(
                self,
                f"Td{cid}",
                cpu=512,
                memory_limit_mib=1024,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.X86_64
                ),
            )
            td.add_container(
                "app",
                image=ecs.ContainerImage.from_docker_image_asset(api_image),
                command=["python", "worker.py"],
                environment={
                    **db_env,
                    **otel_env,
                    **data_key_env,
                    "JOB_BACKEND": "sqs",
                    "JOB_QUEUE_URL": queue_.queue_url,
                    "WORKER_QUEUE_KIND": kind,
                    "REDIS_URL": redis_url,
                    "ORCHESTRATOR_MODE": "agentcore",
                    "A2A_WATCH_URL": "",  # resolved from SSM /argus/watch-a2a-url (agents stack)
                    "A2A_AUTH": "sigv4",
                    "SWEEP_INTERVAL_MIN": "0",  # EventBridge Scheduler enqueues sweeps on AWS
                    "SWEEP_HOURS": "12",
                    "WORKER_CONCURRENCY": str(concurrency),
                    "SCENARIO_END": scenario_end,
                    "AWS_REGION": self.region,
                },
                secrets=db_secret,
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=cid.lower(), log_group=log_group
                ),
            )
            for stmt in (
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    resources=[
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter/argus/*"
                    ],
                ),
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"
                    ],
                ),
                # Watch is reached through the agents gateway (agents stack, ADR-0013); the
                # gateway ARN is unknown here, so the grant is on this account's gateways.
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                    ],
                ),
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
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        data.data_key_secret.secret_arn,
                        data.db_secret.secret_arn,
                    ],
                ),
                iam.PolicyStatement(
                    actions=["kms:Decrypt"], resources=[data.kms_key.key_arn]
                ),
            ):
                td.task_role.add_to_policy(stmt)
            queue_.grant_consume_messages(td.task_role)
            queue_.grant_send_messages(td.task_role)  # retries with delay
            return ecs.FargateService(
                self,
                f"Svc{cid}",
                cluster=cluster,
                task_definition=td,
                desired_count=desired,
                min_healthy_percent=0,
                max_healthy_percent=200,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                security_groups=[services_sg],
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            )

        plain_services["worker"] = worker_service(
            "Worker", jobs_queue, "investigation", 4
        )
        plain_services["sweep-worker"] = worker_service(
            "SweepWorker", sweeps_queue, "sweep", 1
        )

        # ---- scheduled sweeps: EventBridge Scheduler -> SQS, on the worker's idempotency key ----
        schedule_dlq = sqs.Queue(
            self,
            "SweepScheduleDlq",
            queue_name="argus-sweep-schedule-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data.kms_key,
            enforce_ssl=True,
        )
        # The worker derives the same key from the message, so a duplicate firing is a no-op.
        scheduler.Schedule(
            self,
            "SweepSchedule",
            schedule=scheduler.ScheduleExpression.rate(Duration.minutes(sweep_minutes)),
            target=scheduler_targets.SqsSendMessage(
                sweeps_queue,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"schedule": "sweep", "hours": 12, "interval_min": sweep_minutes}
                ),
                # a delivery the scheduler cannot make is retried, then parked, never dropped
                dead_letter_queue=schedule_dlq,
                retry_attempts=3,
                max_event_age=Duration.minutes(15),
            ),
            enabled=not paused,
            description="Argus watch sweep",
        )

        # ---- AIS replay (one task, streams the scenario) or live AISStream ingest ----
        replay_secrets = dict(db_secret)
        if ais_mode == "live":
            replay_secrets["AISSTREAM_API_KEY"] = ecs.Secret.from_secrets_manager(
                feed_secret("AisStreamKey", "AISStream API key for live AIS ingest")
            )
        replay_td, replay_svc = service(
            "ais-replay",
            replay_image,
            8000,
            {
                **db_env,
                "REDIS_URL": redis_url,
                "AIS_MODE": ais_mode,
                "WATCH_AREAS": watch_areas,
                "REPLAY_SPEED": "60",
                "EXTRA_DATABASES": ",".join(["grafana"] if grafana_stack else []),
                **data_key_env,
            },
            replay_secrets,
            cpu=256,
            mem=512,
        )
        # The ingest task reports on itself (Argus/Feed): the alarm on LastPositionAge is
        # the only signal that catches an expired feed key or a stalled subscription,
        # because the task stays RUNNING and healthy either way (I4).
        replay_td.task_role.add_to_policy(
            iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"])
        )

        # The replay task creates the consoles' databases at start; they migrate them. Tasks
        # must also not start before the EFS mount targets exist, or the volume mount fails.
        if observability is not None:
            observability.node.add_dependency(replay_svc)
            observability.node.add_dependency(obs_fs.mount_targets_available)

        # ---- positions archiver: daily scheduled task (ADR-0005) ----
        archiver_image = img(
            "Archiver", "services/archiver/Dockerfile", ["services/archiver"]
        )
        archiver = ecs_patterns.ScheduledFargateTask(
            self,
            "Archiver",
            cluster=cluster,
            schedule=appscaling.Schedule.cron(minute="15", hour="2"),
            desired_task_count=1,
            enabled=not paused,  # the schedule is switched off in stop mode
            subnet_selection=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[services_sg],
            scheduled_fargate_task_image_options=ecs_patterns.ScheduledFargateTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(archiver_image),
                cpu=512,
                memory_limit_mib=2048,
                environment={
                    **db_env,
                    "ARCHIVE_BUCKET": data.archive_bucket.bucket_name,
                    "ARCHIVE_KMS_KEY_ID": data.kms_key.key_id,
                    "AWS_REGION": self.region,
                },
                secrets=db_secret,
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="archiver", log_group=log_group
                ),
            ),
        )
        archiver.task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
                resources=[
                    data.archive_bucket.bucket_arn,
                    f"{data.archive_bucket.bucket_arn}/*",
                ],
            )
        )
        archiver.task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"],
                resources=[data.kms_key.key_arn],
            )
        )

        # ---- UI on a public ALB ----
        # The UI container listens on 8080, not 80: it runs as a non-root user and a
        # non-root process cannot bind a privileged port (I20). The balancer's own
        # listeners are unchanged.
        _, ui_svc = service(
            "ui",
            ui_image,
            8080,
            {},
            cpu=256,
            mem=512,
            health="/",
            extra_sgs=[ui_sg],
            replicas=2,
        )
        # nginx resolves `api` through Service Connect at start, and Service Connect only
        # injects names of services that existed when the task started.
        ui_svc.node.add_dependency(api_svc)
        ui_sg.add_ingress_rule(
            public_alb.connections.security_groups[0],
            ec2.Port.tcp(8080),
            "public ALB to UI",
        )
        allowed = ec2.Peer.ipv4(ui_cidr)
        # Port 80 is a redirect listener and the UI target group is its child: both keep
        # stable logical ids, so the ECS service's load-balancer binding never changes and
        # a listener change updates the service in place instead of replacing it.
        http = public_alb.add_listener(
            "Http",
            port=80,
            open=False,
            default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True
            ),
        )
        ui_tg = elbv2.ApplicationTargetGroup(
            http,
            "UiGroup",
            vpc=vpc,
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[ui_svc],
            health_check=elbv2.HealthCheck(path="/"),
        )
        target_groups["ui"] = ui_tg
        certificates = [elbv2.ListenerCertificate.from_arn(cert_arn)]
        # HTTPS only (the sign-in action exists only on HTTPS listeners); HTTP redirects.
        https = public_alb.add_listener(
            "Https",
            port=443,
            open=False,
            certificates=certificates,
            ssl_policy=elbv2.SslPolicy.TLS13_RES,
            default_action=sign_in.authenticate(elbv2.ListenerAction.forward([ui_tg])),
        )
        bearer_bypass(https, "ApiBearer", ui_tg)
        public_alb.connections.allow_from(allowed, ec2.Port.tcp(443), "watch floor")
        public_alb.connections.allow_from(allowed, ec2.Port.tcp(80), "redirect")
        allow_oidc_egress(public_alb)
        web_acl = public_web_acl(self, "WebAcl", alb=public_alb)
        ui_url = f"https://{watch_host}"

        if observability is not None:
            gl = public_alb.add_listener(
                "Grafana",
                port=3000,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                open=False,
                certificates=certificates,
                ssl_policy=elbv2.SslPolicy.TLS13_RES,
            )
            grafana_tg = elbv2.ApplicationTargetGroup(
                gl,
                "GrafanaGroup",
                vpc=vpc,
                port=3000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                targets=[
                    observability.load_balancer_target(
                        container_name="grafana", container_port=3000
                    )
                ],
                health_check=elbv2.HealthCheck(path="/api/health"),
            )
            target_groups["grafana"] = grafana_tg
            gl.add_action(
                "Default",
                action=sign_in.authenticate(elbv2.ListenerAction.forward([grafana_tg])),
            )
            public_alb.connections.allow_from(allowed, ec2.Port.tcp(3000), "grafana")
            CfnOutput(self, "GrafanaUrl", value=f"https://{watch_host}:3000")

        CfnOutput(self, "AlertsTopicArn", value=topic.topic_arn)

        # Age thresholds are the queue's whole timeout ladder (job timeout + visibility
        # margin + reaper grace, jobqueue.py) so a redelivered job never trips them.
        alarms = PlatformAlarms(
            self,
            "Alarms",
            topic=topic,
            paused=paused,
            queues={"jobs": (jobs_queue, 2400, 50), "sweeps": (sweeps_queue, 1500, 3)},
            dlqs={**dlqs, "sweep-schedule": schedule_dlq},
            albs={"public": public_alb, "internal": internal_alb},
            target_groups=target_groups,
            # The UI is registered on the public ALB after service() ran: alarm on its target.
            services={
                k: v for k, v in plain_services.items() if k not in target_groups
            },
            cluster=cluster,
            aurora=data.cluster,
            nat_gateway_ids=nat_gateway_ids,
            web_acl_name=web_acl.name,
            run_metrics=True,
            user_pool_id=sign_in.pool.user_pool_id,
            ais_mode=ais_mode,
        )
        CfnOutput(self, "AlarmCount", value=str(len(alarms.names)))

        # ---- exports consumed by the agents stack ----
        self.api_url = f"https://{self.internal_alb_dns}:8000"
        self.collector_url = f"http://{self.internal_alb_dns}:4318"
        self.collector_endpoint = f"http://{self.internal_alb_dns}:4318"
        CfnOutput(self, "UiUrl", value=ui_url)
        CfnOutput(self, "InternalAlbDns", value=self.internal_alb_dns)
