"""CloudWatch alarms for the layer under the application SLOs.

Grafana rules (observability/aws/alerting.yaml) watch the API's own metrics: feed lag,
completion, spend, review backlog, rejections, eval gate, scrape loss. They all go dark
together when the API, the collector or Grafana is down, so the signals that must page
regardless live here as CloudWatch alarms on AWS-side metrics: queues and dead letters,
load balancers and their targets, running tasks, Aurora, NAT, and the eval gate as pushed
by `evals/node_evals.py --push`. Alarm and recovery both notify the one SNS topic.

Availability alarms (no healthy target, no running task) are skipped in stop mode
(`-c paused=true`): every service is at zero on purpose then."""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

EVAL_SUITES = ("watch", "investigator", "tasking", "report")
GIB = 1024**3


class PlatformAlarms(Construct):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        topic: sns.ITopic,
        paused: bool,
        queues: dict[str, tuple[sqs.Queue, int, int]],
        dlqs: dict[str, sqs.IQueue],
        albs: dict[str, elbv2.ApplicationLoadBalancer],
        target_groups: dict[str, elbv2.ApplicationTargetGroup],
        services: dict[str, ecs.FargateService],
        cluster: ecs.Cluster,
        aurora: rds.DatabaseCluster,
        nat_gateway_ids: list[str],
        web_acl_name: str = "",
        run_metrics: bool = False,
        user_pool_id: str = "",
        ais_mode: str = "live",
    ):
        super().__init__(scope, cid)
        self.topic = topic
        self.names: list[str] = []
        five = Duration.minutes(5)
        one = Duration.minutes(1)

        # ---- jobs: a message older than its job's whole timeout ladder means the worker
        # is not draining, a growing backlog means it cannot keep up.
        for label, (queue, max_age_s, backlog) in queues.items():
            self.alarm(
                f"argus-{label}-oldest-message",
                queue.metric_approximate_age_of_oldest_message(
                    period=five, statistic="Maximum"
                ),
                max_age_s,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                f"oldest {label} message older than {max_age_s}s: workers not draining",
            )
            self.alarm(
                f"argus-{label}-backlog",
                queue.metric_approximate_number_of_messages_visible(
                    period=five, statistic="Maximum"
                ),
                backlog,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                f"more than {backlog} {label} waiting for fifteen minutes",
                periods=3,
            )
        for label, dlq in dlqs.items():
            self.alarm(
                f"argus-{label}-dlq",
                dlq.metric_approximate_number_of_messages_visible(
                    period=five, statistic="Maximum"
                ),
                0,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                f"a {label} message was dead-lettered",
            )

        # ---- load balancers: 5xx from the balancer itself (no target) and from targets.
        for label, alb in albs.items():
            self.alarm(
                f"argus-{label}-alb-5xx",
                alb.metrics.http_code_elb(
                    elbv2.HttpCodeElb.ELB_5XX_COUNT, period=five, statistic="Sum"
                ),
                10,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                f"{label} ALB answered 5xx itself (no healthy target or timeouts)",
                periods=2,
            )
            self.alarm(
                f"argus-{label}-alb-target-5xx",
                alb.metrics.http_code_target(
                    elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=five, statistic="Sum"
                ),
                10,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                f"targets behind the {label} ALB returned 5xx",
                periods=2,
            )
        # ---- the sign-in itself (I10). A callback URL changed by a redeploy, or a
        # broken Cognito integration, locks every officer out of the watch floor while
        # the balancer and its targets stay perfectly healthy, so nothing else pages.
        if "public" in albs:
            self.alarm(
                "argus-alb-auth-errors",
                albs["public"].metrics.custom(
                    "ELBAuthError", statistic="Sum", period=five
                ),
                10,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "the public load balancer could not complete a Cognito sign-in",
            )
        if not paused:
            for label, tg in target_groups.items():
                self.alarm(
                    f"argus-{label}-down",
                    tg.metrics.healthy_host_count(period=one, statistic="Minimum"),
                    1,
                    cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                    f"no healthy {label} target for three minutes",
                    periods=3,
                    missing=cw.TreatMissingData.BREACHING,
                )
            # Services without a load balancer: Container Insights running-task count.
            for label, svc in services.items():
                self.alarm(
                    f"argus-{label}-down",
                    cw.Metric(
                        namespace="ECS/ContainerInsights",
                        metric_name="RunningTaskCount",
                        dimensions_map={
                            "ClusterName": cluster.cluster_name,
                            "ServiceName": svc.service_name,
                        },
                        period=one,
                        statistic="Minimum",
                    ),
                    1,
                    cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                    f"no running {label} task for five minutes",
                    periods=5,
                    missing=cw.TreatMissingData.BREACHING,
                )

        # ---- Aurora Serverless v2: CPU, capacity against the configured maximum, and the
        # local (temporary) storage that a runaway query can exhaust.
        self.alarm(
            "argus-aurora-cpu",
            aurora.metric_cpu_utilization(period=five, statistic="Average"),
            80,
            cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            "Aurora CPU above 80% for fifteen minutes",
            periods=3,
        )
        self.alarm(
            "argus-aurora-capacity",
            aurora.metric("ACUUtilization", period=five, statistic="Average"),
            90,
            cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            "Aurora at 90% of its maximum ACUs for fifteen minutes",
            periods=3,
        )
        self.alarm(
            "argus-aurora-local-storage",
            aurora.metric("FreeLocalStorage", period=five, statistic="Minimum"),
            2 * GIB,
            cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            "Aurora local storage under 2 GiB",
        )

        # ---- NAT: the two counters AWS recommends alarming on.
        for i, nat_id in enumerate(nat_gateway_ids):
            for metric, what in (
                ("ErrorPortAllocation", "port-allocation"),
                ("PacketsDropCount", "packets-dropped"),
            ):
                self.alarm(
                    f"argus-nat-{i}-{what}",
                    cw.Metric(
                        namespace="AWS/NATGateway",
                        metric_name=metric,
                        dimensions_map={"NatGatewayId": nat_id},
                        period=five,
                        statistic="Sum",
                    ),
                    0,
                    cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                    f"NAT gateway {i} reported {metric}",
                )

        # ---- WAF: a surge of blocked requests is an attack or a rule blocking officers.
        if web_acl_name:
            self.alarm(
                "argus-waf-blocked",
                cw.Metric(
                    namespace="AWS/WAFV2",
                    metric_name="BlockedRequests",
                    dimensions_map={
                        "WebACL": web_acl_name,
                        "Region": Stack.of(self).region,
                        "Rule": "ALL",
                    },
                    period=five,
                    statistic="Sum",
                ),
                100,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "the web ACL blocked more than 100 requests in five minutes",
            )

        # ---- investigations: branches that degraded (API run_metrics.py). One a day is
        # a throttle; three in an hour is a broken tool or model.
        if run_metrics:
            self.alarm(
                "argus-degraded-branches",
                cw.Metric(
                    namespace="Argus/Investigations",
                    metric_name="DegradedBranches",
                    period=Duration.hours(1),
                    statistic="Sum",
                ),
                2,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "more than two Investigator branches degraded in an hour",
            )

        # ---- the feed itself (I4). The ingest task reconnects forever and stays
        # RUNNING with a healthy container, so an expired AISStream key or a
        # subscription that matches nothing silences the feed with nothing to see in
        # ECS. The task publishes its own age since the last stored position; missing
        # data breaches, so the alarm also fires when the task stops publishing at all.
        if not paused:
            self.alarm(
                "argus-feed-stalled",
                cw.Metric(
                    namespace="Argus/Feed",
                    metric_name="LastPositionAge",
                    dimensions_map={"mode": ais_mode},
                    period=five,
                    statistic="Maximum",
                ),
                900,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "no AIS position stored for fifteen minutes, or the ingest task stopped reporting",
                periods=2,
                missing=cw.TreatMissingData.BREACHING,
            )

        # ---- officers cannot sign in (I12). Cognito counts sign-in failures per pool;
        # a burst means the pool, the balancer's authenticate action or the officers'
        # credentials are broken, and the watch floor is shut out.
        if user_pool_id:
            self.alarm(
                "argus-sign-in-failures",
                cw.Metric(
                    namespace="AWS/Cognito",
                    metric_name="SignInThrottles",
                    dimensions_map={"UserPool": user_pool_id, "UserPoolClient": "ALL"},
                    period=five,
                    statistic="Sum",
                ),
                5,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "Cognito is throttling or refusing sign-ins: officers may be locked out",
            )

        # ---- a deployment rolled back (I12). The circuit breaker rolls a crash-looping
        # task back on its own, which is the right behaviour and an invisible one: the
        # stack reports success and the old task definition keeps serving.
        if not paused:
            self.alarm(
                "argus-deployment-rollbacks",
                cw.Metric(
                    namespace="AWS/ECS",
                    metric_name="ServiceDeploymentsFailed",
                    dimensions_map={"ClusterName": cluster.cluster_name},
                    period=Duration.minutes(15),
                    statistic="Sum",
                ),
                0,
                cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                "a service deployment failed and the circuit breaker rolled it back",
            )

        # ---- sweeps that find nothing (I12). A sweep that raises no alert is normal;
        # a day of them means the detectors, the tool plane or the feed are broken in a
        # way that still returns success. Six hours of zero is the signal.
        if run_metrics and not paused:
            self.alarm(
                "argus-sweeps-raising-nothing",
                cw.Metric(
                    namespace="Argus/Sweeps",
                    metric_name="AlertsRaised",
                    period=Duration.hours(6),
                    statistic="Sum",
                ),
                1,
                cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                "no alert raised by any sweep in six hours: check the detectors and the feed",
                missing=cw.TreatMissingData.BREACHING,
            )

        # ---- eval gate: `node_evals.py --push` publishes gate_passed per suite (0 or 1).
        for suite in EVAL_SUITES:
            self.alarm(
                f"argus-eval-{suite}",
                cw.Metric(
                    namespace="Argus/Evals",
                    metric_name="gate_passed",
                    dimensions_map={"suite": suite},
                    period=Duration.hours(1),
                    statistic="Minimum",
                ),
                1,
                cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                f"the latest {suite} eval run missed a floor in evals/thresholds.yaml, "
                "or no run has reported",
                # A gate that never ran must not read as a gate that passed (I12).
                missing=cw.TreatMissingData.BREACHING,
            )

    def alarm(
        self,
        name: str,
        metric: cw.IMetric,
        threshold: float,
        op: cw.ComparisonOperator,
        description: str,
        *,
        periods: int = 1,
        missing: cw.TreatMissingData = cw.TreatMissingData.NOT_BREACHING,
    ) -> cw.Alarm:
        a = cw.Alarm(
            self,
            name,
            alarm_name=name,
            alarm_description=f"{description} (docs/RUNBOOK.md#alarms)",
            metric=metric,
            threshold=threshold,
            comparison_operator=op,
            evaluation_periods=periods,
            treat_missing_data=missing,
        )
        a.add_alarm_action(cw_actions.SnsAction(self.topic))
        a.add_ok_action(cw_actions.SnsAction(self.topic))
        self.names.append(name)
        return a
