"""AgentCore Evaluations: online scoring of live investigations. Built-in evaluators for
response quality, tool use and safety, plus the report rubric the local eval gate already
uses, as a custom LLM-as-judge evaluator on the strong tier. Results land in CloudWatch
(`Bedrock-AgentCore/Evaluations` metrics and an evaluations log group), which Grafana
reads through its CloudWatch datasource. Requires CloudWatch Transaction Search and the
agents' unified telemetry, both set up by the platform and agents stacks."""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam

BUILTIN_EVALUATORS = (
    "Builtin.Helpfulness",
    "Builtin.Correctness",
    "Builtin.InstructionFollowing",
    "Builtin.ToolSelectionAccuracy",
    "Builtin.ToolParameterAccuracy",
    "Builtin.GoalSuccessRate",
    "Builtin.Harmfulness",
)

REPORT_RUBRIC = """You are a senior maritime watch officer reviewing an AI-produced Vessel of Interest report at the end of an investigation session.
Rate the session's final report on one 1-5 scale, weighing five dimensions equally:
- bottom line up front: the headline states what the vessel did, how sure we are and what should happen next;
- traceability: every indicator rests on cited tool evidence (AIS, registry, geo, imagery) and nothing is invented;
- balance: counter-indicators and information gaps are stated honestly;
- actionability: recommended actions are advisory steps a watch officer can take (monitor, query the flag state, request an inspection, propose imagery), never actions the agent claims to take itself;
- clarity: a watch officer can read it in ninety seconds.
Score 5 only when all five hold; 1 when the report is unusable or unsupported.

Session to review (every turn, tool call and the final report):
{context}"""

# A SESSION-level evaluator must reference at least one session placeholder
# (available_tools, context, actual_tool_trajectory, expected_tool_trajectory, assertions);
# CreateEvaluator rejects instructions without one.
SESSION_PLACEHOLDERS = (
    "{available_tools}",
    "{context}",
    "{actual_tool_trajectory}",
    "{expected_tool_trajectory}",
    "{assertions}",
)


def build_evaluations(
    stack: Stack,
    *,
    agents: dict[str, tuple[str, str]],
    judge_model_id: str,
    sampling_percentage: float = 50.0,
) -> tuple[agentcore.CfnEvaluator, dict[str, agentcore.CfnOnlineEvaluationConfig]]:
    """One evaluator (the report rubric) and one online configuration per agent: a
    configuration reads exactly one service name. `agents` maps the agent name to its
    (runtime log group, service name)."""
    log_group_names = [g for g, _ in agents.values()]
    role = iam.Role(
        stack,
        "EvaluationRole",
        role_name="argus-agentcore-evaluations",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": stack.account,
                    "aws:ResourceAccount": stack.account,
                },
                "ArnLike": {
                    "aws:SourceArn": [
                        f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:evaluator/*",
                        f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:online-evaluation-config/*",
                    ]
                },
            },
        ),
        description="AgentCore Evaluations: read traces, write results, call the judge model",
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:DescribeLogGroups",
                "logs:GetQueryResults",
                "logs:StartQuery",
            ],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            resources=[
                f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/evaluations/*"
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
            resources=[
                f"arn:aws:logs:{stack.region}:{stack.account}:log-group:aws/spans",
                f"arn:aws:logs:{stack.region}:{stack.account}:log-group:aws/spans:*",
            ]
            + [
                f"arn:aws:logs:{stack.region}:{stack.account}:log-group:{g}"
                for g in log_group_names
            ]
            + [
                f"arn:aws:logs:{stack.region}:{stack.account}:log-group:{g}:*"
                for g in log_group_names
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{stack.region}::foundation-model/*",
                f"arn:aws:bedrock:{stack.region}:{stack.account}:inference-profile/*",
                "arn:aws:bedrock:*::foundation-model/*",
            ],
        )
    )
    evaluator = agentcore.CfnEvaluator(
        stack,
        "ReportRubricEvaluator",
        evaluator_name="argus_report_rubric",
        description="Argus Vessel of Interest report rubric (BLUF, traceability, balance, actionability, clarity)",
        level="SESSION",
        evaluator_config=agentcore.CfnEvaluator.EvaluatorConfigProperty(
            llm_as_a_judge=agentcore.CfnEvaluator.LlmAsAJudgeEvaluatorConfigProperty(
                instructions=REPORT_RUBRIC,
                model_config=agentcore.CfnEvaluator.EvaluatorModelConfigProperty(
                    bedrock_evaluator_model_config=agentcore.CfnEvaluator.BedrockEvaluatorModelConfigProperty(
                        model_id=judge_model_id
                    )
                ),
                rating_scale=agentcore.CfnEvaluator.RatingScaleProperty(
                    numerical=[
                        agentcore.CfnEvaluator.NumericalScaleDefinitionProperty(
                            value=v, label=label, definition=definition
                        )
                        for v, label, definition in (
                            (
                                1,
                                "unusable",
                                "Unsupported, invented or unreadable; no officer could act on it.",
                            ),
                            (
                                2,
                                "weak",
                                "Several dimensions fail: thin evidence, one-sided, or actions the agent claims to take.",
                            ),
                            (
                                3,
                                "adequate",
                                "Usable with reservations: mostly traceable, some gaps in balance or clarity.",
                            ),
                            (4, "good", "All dimensions hold with minor lapses."),
                            (
                                5,
                                "excellent",
                                "Bottom line up front, every indicator traceable, balanced, advisory actions, readable in ninety seconds.",
                            ),
                        )
                    ]
                ),
            )
        ),
    )
    configs: dict[str, agentcore.CfnOnlineEvaluationConfig] = {}
    for name, (log_group, service_name) in agents.items():
        # The report rubric judges the orchestrator's session (it writes the report); the
        # specialists get the built-in response, tool and safety evaluators.
        evaluators = list(BUILTIN_EVALUATORS)
        refs = [
            agentcore.CfnOnlineEvaluationConfig.EvaluatorReferenceProperty(
                evaluator_id=e
            )
            for e in evaluators
        ]
        if name == "orchestrator":
            refs.append(
                agentcore.CfnOnlineEvaluationConfig.EvaluatorReferenceProperty(
                    evaluator_id=evaluator.attr_evaluator_id
                )
            )
        config = agentcore.CfnOnlineEvaluationConfig(
            stack,
            f"OnlineEvaluation{name.title()}",
            online_evaluation_config_name=f"argus_{name}",
            description=f"Continuous evaluation of the Argus {name} agent",
            data_source_config=agentcore.CfnOnlineEvaluationConfig.DataSourceConfigProperty(
                cloud_watch_logs=agentcore.CfnOnlineEvaluationConfig.CloudWatchLogsInputConfigProperty(
                    log_group_names=[log_group], service_names=[service_name]
                )
            ),
            rule=agentcore.CfnOnlineEvaluationConfig.RuleProperty(
                sampling_config=agentcore.CfnOnlineEvaluationConfig.SamplingConfigProperty(
                    sampling_percentage=sampling_percentage
                ),
                session_config=agentcore.CfnOnlineEvaluationConfig.SessionConfigProperty(
                    session_timeout_minutes=15
                ),
            ),
            evaluators=refs,
            evaluation_execution_role_arn=role.role_arn,
            execution_status="ENABLED",
        )
        config.node.add_dependency(evaluator)
        config.node.add_dependency(role)
        configs[name] = config
    return evaluator, configs
