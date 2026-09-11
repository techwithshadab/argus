"""Data: Aurora Serverless v2 PostgreSQL (PostGIS) and ElastiCache Serverless (Valkey).

`-c paused=true` lets Aurora scale to 0 ACU and auto-pause; the running configuration keeps a
0.5 ACU floor so sweeps do not pay a resume latency."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_backup as backup
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_events as events
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

from .lifecycle import is_paused


def _flag(value, default: bool) -> bool:
    """A context flag, with an explicit default. Unset means the default, not false."""
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes")


class DataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        vpc: ec2.Vpc,
        services_sg: ec2.SecurityGroup,
        **kw,
    ):
        super().__init__(scope, cid, **kw)
        paused = is_paused(self)
        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            description="Aurora PostgreSQL",
            allow_all_outbound=False,
        )
        db_sg.add_ingress_rule(services_sg, ec2.Port.tcp(5432), "services to postgres")

        # Records are kept unless someone says otherwise: the runbook and the security
        # page promise seven-year retention, and the flag defaulted to false, so a
        # `make destroy` deleted the evidence behind every past investigation without
        # a prompt. `-c retainData=false` is now the deliberate act, and it is what
        # `make destroy` passes (I1).
        retain_data = _flag(self.node.try_get_context("retainData"), default=True)
        reader = str(self.node.try_get_context("auroraReader") or "").lower() in (
            "true",
            "yes",
        )
        self.cluster = rds.DatabaseCluster(
            self,
            "Aurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_17_4
            ),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            readers=(
                [rds.ClusterInstance.serverless_v2("reader", scale_with_writer=True)]
                if reader
                else None
            ),
            serverless_v2_min_capacity=0 if paused else 0.5,
            serverless_v2_max_capacity=4,
            serverless_v2_auto_pause_duration=Duration.minutes(15) if paused else None,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[db_sg],
            default_database_name="argus",
            credentials=rds.Credentials.from_generated_secret("argus"),
            storage_encrypted=True,
            iam_authentication=True,
            backup=rds.BackupProps(retention=Duration.days(7)),
            removal_policy=(
                RemovalPolicy.SNAPSHOT if retain_data else RemovalPolicy.DESTROY
            ),
            deletion_protection=retain_data,
        )
        self.db_secret = self.cluster.secret
        # Rotate the database password every 30 days (Secrets Manager hosted rotation in
        # the private subnets). Consumers re-read the secret when a connection fails
        # (services/api/dbconn.py and its copies); Grafana's task is replaced by its
        # health check.
        self.cluster.add_rotation_single_user(automatically_after=Duration.days(30))
        # PostGIS extension is created by the schema script at first start (CREATE EXTENSION postgis is
        # available on Aurora PostgreSQL 17 without extra configuration).

        cache_sg = ec2.SecurityGroup(
            self, "CacheSg", vpc=vpc, description="Valkey", allow_all_outbound=False
        )
        cache_sg.add_ingress_rule(services_sg, ec2.Port.tcp(6379), "services to valkey")
        self.cache = elasticache.CfnServerlessCache(
            self,
            "Valkey",
            engine="valkey",
            serverless_cache_name="argus-stream",
            subnet_ids=[s.subnet_id for s in vpc.private_subnets],
            security_group_ids=[cache_sg.security_group_id],
        )
        self.cache_endpoint = self.cache.attr_endpoint_address
        self.cache_port = self.cache.attr_endpoint_port

        # ---- backups beyond the seven days Aurora keeps (I2) ----
        # Automated backups are same-region and expire after a week, so a deletion or a
        # regional loss older than that took everything with it while the runbook
        # promised a seven-year posture. The vault carries its own key: the data key
        # below is destroyable, and a vault encrypted with it becomes unreadable a week
        # after a destroy, which would make the monthly rule a lie. `-c backups=false`
        # switches the plan off for a short-lived demo account.
        if _flag(self.node.try_get_context("backups"), default=True):
            vault_key = kms.Key(
                self,
                "BackupKey",
                description="Argus backup vault",
                enable_key_rotation=True,
                removal_policy=RemovalPolicy.RETAIN,
            )
            self.backup_vault = backup.BackupVault(
                self,
                "Vault",
                backup_vault_name="argus-vault",
                encryption_key=vault_key,
                # A vault holding recovery points blocks `cdk destroy`, so it follows
                # the same flag as the cluster: deleting is the deliberate act (I1).
                removal_policy=(
                    RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY
                ),
            )
            plan = backup.BackupPlan(
                self,
                "BackupPlan",
                backup_plan_name="argus",
                backup_vault=self.backup_vault,
            )
            plan.add_rule(
                backup.BackupPlanRule(
                    rule_name="daily-35d",
                    schedule_expression=events.Schedule.cron(hour="5", minute="0"),
                    delete_after=Duration.days(35),
                )
            )
            plan.add_rule(
                backup.BackupPlanRule(
                    rule_name="monthly-7y",
                    schedule_expression=events.Schedule.cron(
                        day="1", hour="6", minute="0"
                    ),
                    # Cold storage needs at least 90 days of retention, so it belongs
                    # on the monthly rule only.
                    move_to_cold_storage_after=Duration.days(90),
                    delete_after=Duration.days(7 * 365 + 2),
                )
            )
            plan.add_selection(
                "Aurora",
                resources=[
                    backup.BackupResource.from_rds_database_cluster(self.cluster)
                ],
            )

        # ---- personal-data encryption (phase 2): one KMS key, one data key in Secrets Manager ----
        # Services read the data key once at startup (datakey.py) and use it with pgcrypto for the
        # columns that hold personal data. Rotating the secret re-keys nothing by itself; that is a
        # deliberate manual step (decrypt with old, encrypt with new).
        self.kms_key = kms.Key(
            self,
            "DataKey",
            description="Argus data key: Secrets Manager data key and the positions archive",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        self.data_key_secret = sm.Secret(
            self,
            "DataKeySecret",
            secret_name="argus/data-key",
            description="Symmetric key for column-level encryption of personal data (pgcrypto)",
            encryption_key=self.kms_key,
            generate_secret_string=sm.SecretStringGenerator(
                password_length=48, exclude_punctuation=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- positions archive (ADR-0005): daily partitions older than the hot window, as Parquet ----
        retain = _flag(self.node.try_get_context("retainArchive"), default=True)
        # Access logs of the two load balancers and the archive bucket, 90 days. ALB log
        # delivery needs SSE-S3, not KMS.
        self.logs_bucket = s3.Bucket(
            self,
            "AccessLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.archive_bucket = s3.Bucket(
            self,
            "PositionsArchive",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="archive/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(30),
                        ),
                        s3.Transition(
                            storage_class=s3.StorageClass.DEEP_ARCHIVE,
                            transition_after=Duration.days(365),
                        ),
                    ],
                    # Seven years, the retention class of the archive (docs/RUNBOOK.md).
                    expiration=Duration.days(7 * 365 + 2),
                    noncurrent_version_expiration=Duration.days(30),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN if retain else RemovalPolicy.DESTROY,
            auto_delete_objects=not retain,
        )
