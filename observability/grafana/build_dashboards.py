#!/usr/bin/env python3
"""Generate the Grafana board from a single description so thresholds, links and
datasources stay consistent. One board, `argus`: a KPI strip, the watch floor open, and
collapsed sections for pipeline health, agents and tools, one investigation, models and
cost, and (AWS) AgentCore, Bedrock and AWS resources. Writes the local variant (Prometheus,
Tempo, Loki) to observability/grafana/dashboards and the AWS variant (plus CloudWatch
panels) to observability/aws/dashboards. Run: python observability/grafana/build_dashboards.py

The per-persona builders below are the panel library the board is assembled from; every
query is defined once there (docs/RUNBOOK.md, "The board").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = HERE / "dashboards"
AWS = HERE.parent / "aws" / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
TEMPO = {"type": "tempo", "uid": "tempo"}
LOKI = {"type": "loki", "uid": "loki"}
CW = {"type": "cloudwatch", "uid": "cloudwatch"}

# Every threshold is the SLO target the API also exports (argus_target_*) or an operator's
# judgement written down once here.
T = {
    "feed_lag_s": (300, 900),
    "oldest_unreviewed_min": (60, 240),
    "sweep_p95_s": (200, 300),
    "investigation_p95_s": (400, 600),
    "completion": (0.95, 0.9),  # green above, red below
    "lag_p95_s": (60, 120),
    "cost_today_usd": (5, 10),
    "eval_recall": (0.9, 0.8),
}


def thresholds(warn, crit, invert=False):
    """Green → amber → red as the value crosses warn then crit; inverted for 'higher is
    better' ratios."""
    if invert:
        return {
            "mode": "absolute",
            "steps": [
                {"color": "red", "value": None},
                {"color": "orange", "value": crit},
                {"color": "green", "value": warn},
            ],
        }
    return {
        "mode": "absolute",
        "steps": [
            {"color": "green", "value": None},
            {"color": "orange", "value": warn},
            {"color": "red", "value": crit},
        ],
    }


_id = 0


def panel(
    kind,
    title,
    targets,
    x,
    y,
    w,
    h,
    *,
    unit=None,
    thr=None,
    desc="",
    extra=None,
    ds=PROM,
):
    global _id
    _id += 1
    p = {
        "id": _id,
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": ds,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"refId": chr(65 + i), "datasource": ds, **t} for i, t in enumerate(targets)
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if thr:
        p["fieldConfig"]["defaults"]["thresholds"] = thr
        if kind == "timeseries":
            p["fieldConfig"]["defaults"]["custom"] = {
                "thresholdsStyle": {"mode": "dashed+area"}
            }
    if kind == "stat":
        p["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "area",
            "textMode": "value",
        }
    if kind == "timeseries":
        p["options"] = {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        }
    if kind == "bargauge":
        p["options"] = {
            "orientation": "horizontal",
            "displayMode": "gradient",
            "reduceOptions": {"calcs": ["lastNotNull"]},
        }
    if extra:
        p.update(extra)
    return p


def row(title, y):
    global _id
    _id += 1
    return {
        "id": _id,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def prom(expr, legend=""):
    return {
        "expr": expr,
        "legendFormat": legend or "__auto",
        "range": True,
        "instant": False,
    }


def prom_instant(expr, legend=""):
    return {
        "expr": expr,
        "legendFormat": legend or "__auto",
        "instant": True,
        "range": False,
    }


def loki(expr, legend=""):
    return {"expr": expr, "legendFormat": legend, "queryType": "range"}


def traceql(q):
    return {"query": q, "queryType": "traceql", "limit": 20, "tableType": "traces"}


def cw_label(label):
    """CloudWatch legend: `{{Dimension}}` in the panel library becomes Grafana's dynamic
    label `${PROP('Dim.Dimension')}`. The plugin stopped expanding the brace form with
    dynamic labels (Grafana 10+), so the legend showed the placeholder itself."""
    return re.sub(r"\{\{(\w+)\}\}", r"${PROP('Dim.\1')}", label)


def cw(
    namespace,
    metric,
    dims,
    stat="Sum",
    period="60",
    label="",
    region="default",
    exact=False,
):
    """One CloudWatch metric query. `exact=True` matches only metrics whose dimension set
    is exactly `dims` (a rollup schema), which is how a panel gets one series per name
    instead of one per resource id, method and protocol."""
    return {
        "queryMode": "Metrics",
        "namespace": namespace,
        "metricName": metric,
        "dimensions": dims,
        "statistic": stat,
        "period": period,
        "region": region,
        "label": cw_label(label),
        "metricQueryType": 0,
        "metricEditorMode": 0,
        "matchExact": exact,
    }


def cw_math(expression, label, stat="Sum", period="300", region="default"):
    """A metric-math query with a fixed legend label (code mode)."""
    return {
        "queryMode": "Metrics",
        "region": region,
        "metricQueryType": 0,
        "metricEditorMode": 1,
        "expression": expression,
        "label": label,
        "period": period,
        "statistic": stat,
        "namespace": "",
        "metricName": "",
        "dimensions": {},
        "matchExact": False,
    }


def rename_series(*pairs):
    """Panel transformation: rename series display names by regex, first match wins."""
    return {
        "transformations": [
            {"id": "renameByRegex", "options": {"regex": rx, "renamePattern": to}}
            for rx, to in pairs
        ]
    }


# AgentCore runtimes as CloudWatch names them (`<runtime>::<endpoint>`), with the legend
# label the board uses. Old runtime ids from earlier deployments carry the same name, so
# the panels sum over the Resource dimension instead of listing one series per id.
AGENTCORE_RUNTIMES = [
    ("argus_watch", "agent · watch"),
    ("argus_investigator", "agent · investigator"),
    ("argus_tasking", "agent · tasking"),
    ("argus_orchestrator", "agent · orchestrator"),
    ("argus_tool_ais", "tool server · ais"),
    ("argus_tool_geo", "tool server · geo"),
    ("argus_tool_imagery", "tool server · imagery"),
    ("argus_tool_registry", "tool server · registry"),
]
ECS_SERVICE_RENAME = (r"^argus-platform-Svc(\w+?)Service[0-9A-F]{8}-\w+$", "$1")


def dashboard(
    uid,
    title,
    tags,
    panels,
    variables=None,
    links=None,
    refresh="30s",
    annotations=None,
):
    return {
        "uid": uid,
        "title": title,
        "tags": ["argus", *tags],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "graphTooltip": 1,
        "refresh": refresh,
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": variables or []},
        "links": links or [],
        "annotations": {"list": annotations or []},
        "panels": panels,
    }


def deploy_annotations():
    return [
        {
            "name": "Deploys",
            "datasource": LOKI,
            "enable": True,
            "iconColor": "#0E7490",
            "expr": '{service_name="api"} |= "Uvicorn running"',
            "titleFormat": "API (re)started",
        }
    ]


def persona_links():
    return [
        {
            "title": "Operations",
            "type": "link",
            "url": "/d/argus-operator",
            "icon": "external link",
        },
        {
            "title": "Investigation",
            "type": "link",
            "url": "/d/argus-analyst",
            "icon": "external link",
        },
        {
            "title": "Models and evals",
            "type": "link",
            "url": "/d/argus-model-owner",
            "icon": "external link",
        },
        {
            "title": "Watch floor",
            "type": "link",
            "url": "/d/argus-watch-floor",
            "icon": "external link",
        },
    ]


# ------------------------------------------------------------------------------ operator
def operator(aws: bool):
    global _id
    _id = 0
    P = []
    y = 0
    P.append(row("Now", y))
    y += 1
    P += [
        panel(
            "stat",
            "Feed lag",
            [prom_instant("argus_feed_lag_s")],
            0,
            y,
            4,
            4,
            unit="s",
            thr=thresholds(*T["feed_lag_s"]),
            desc="Seconds since the newest AIS position. Live mode: AISStream; replay: the loop clock.",
        ),
        panel(
            "stat",
            "Vessels reporting (10 m)",
            [prom_instant("argus_vessels_reporting_10m")],
            4,
            y,
            4,
            4,
            thr=thresholds(1, 0, invert=True),
        ),
        panel(
            "stat",
            "Alerts awaiting review",
            [prom_instant("argus_alerts_awaiting_review")],
            8,
            y,
            4,
            4,
            thr=thresholds(10, 25),
        ),
        panel(
            "stat",
            "Oldest unreviewed alert",
            [prom_instant("argus_oldest_unreviewed_alert_s / 60")],
            12,
            y,
            4,
            4,
            unit="m",
            thr=thresholds(*T["oldest_unreviewed_min"]),
        ),
        panel(
            "stat",
            "Investigations running",
            [prom_instant('argus_investigations_by_status{status="running"}')],
            16,
            y,
            4,
            4,
            thr=thresholds(4, 8),
        ),
        panel(
            "stat",
            "Failed today",
            [
                prom_instant(
                    'sum(argus_investigations_by_status{status="failed"}) or vector(0)'
                )
            ],
            20,
            y,
            4,
            4,
            thr=thresholds(1, 3),
        ),
    ]
    y += 4
    P.append(row("Service levels against their targets (7-day windows)", y))
    y += 1
    P += [
        panel(
            "timeseries",
            "Sweep latency p95",
            [
                prom("argus_sweep_latency_p95_s", "p95"),
                prom("argus_target_sweep_latency_p95_s", "target"),
            ],
            0,
            y,
            8,
            7,
            unit="s",
            thr=thresholds(*T["sweep_p95_s"]),
        ),
        panel(
            "timeseries",
            "Investigation latency p95",
            [
                prom("argus_investigation_latency_p95_s", "p95"),
                prom("argus_target_investigation_latency_p95_s", "target"),
            ],
            8,
            y,
            8,
            7,
            unit="s",
            thr=thresholds(*T["investigation_p95_s"]),
        ),
        panel(
            "timeseries",
            "Alert to investigation lag p95",
            [
                prom("argus_alert_to_investigation_lag_p95_s", "p95"),
                prom("argus_target_alert_to_investigation_lag_p95_s", "target"),
            ],
            16,
            y,
            8,
            7,
            unit="s",
            thr=thresholds(*T["lag_p95_s"]),
        ),
    ]
    y += 7
    P += [
        panel(
            "gauge",
            "Investigation completion",
            [prom_instant("argus_investigation_completion_ratio")],
            0,
            y,
            6,
            6,
            unit="percentunit",
            thr=thresholds(*T["completion"], invert=True),
            desc="Share of investigations that completed in the window; target 90%.",
        ),
        panel(
            "gauge",
            "Error budget burn",
            [
                prom_instant(
                    "(1 - argus_investigation_completion_ratio) / (1 - argus_target_investigation_completion_ratio)"
                )
            ],
            6,
            y,
            6,
            6,
            thr=thresholds(0.8, 1.0),
            desc="1.0 means the whole failure budget for the window is spent.",
        ),
        panel(
            "timeseries",
            "Jobs in the last 24 h",
            [prom("sum by (kind, status) (argus_jobs_24h)", "{{kind}} {{status}}")],
            12,
            y,
            12,
            6,
            extra={
                "fieldConfig": {
                    "defaults": {
                        "custom": {
                            "drawStyle": "bars",
                            "fillOpacity": 60,
                            "stacking": {"mode": "normal"},
                        }
                    },
                    "overrides": [],
                }
            },
        ),
    ]
    y += 6
    P.append(row("Agents and tools", y))
    y += 1
    P += [
        panel(
            "timeseries",
            "Span rate by service",
            [
                prom(
                    "sum by (service_name) (rate(traces_spanmetrics_calls_total[5m]))",
                    "{{service_name}}",
                )
            ],
            0,
            y,
            8,
            7,
            unit="reqps",
        ),
        panel(
            "timeseries",
            "p95 latency by service",
            [
                prom(
                    "histogram_quantile(0.95, sum by (le, service_name) (rate(traces_spanmetrics_latency_bucket[5m])))",
                    "{{service_name}}",
                )
            ],
            8,
            y,
            8,
            7,
            unit="ms",
        ),
        panel(
            "timeseries",
            "Errors in agent logs (5 m)",
            [
                loki(
                    'sum by (service_name) (count_over_time({service_name=~"agent-.*"} |~ "ERROR|Traceback" [5m]))',
                    "{{service_name}}",
                )
            ],
            16,
            y,
            8,
            7,
            ds=LOKI,
        ),
    ]
    y += 7
    P += [
        panel(
            "stat",
            "Node rejections (24 h)",
            [prom_instant("argus_node_rejections_24h")],
            0,
            y,
            4,
            4,
            thr=thresholds(3, 10),
            desc="Report or tasking drafts the policy or schema rejected; each costs a retry.",
        ),
        panel(
            "stat",
            "Tier escalations (24 h)",
            [prom_instant("argus_node_escalations_24h")],
            4,
            y,
            4,
            4,
            thr=thresholds(5, 15),
        ),
        panel(
            "stat",
            "Cost today",
            [prom_instant("argus_investigation_cost_usd_today")],
            8,
            y,
            4,
            4,
            unit="currencyUSD",
            thr=thresholds(*T["cost_today_usd"]),
        ),
        panel(
            "nodeGraph",
            "Service graph",
            [{"queryType": "serviceMap"}],
            12,
            y,
            12,
            8,
            ds=TEMPO,
        ),
    ]
    y += 4
    P.append(
        panel(
            "alertlist",
            "Firing alerts",
            [],
            0,
            y,
            12,
            4,
            extra={
                "options": {
                    "alertName": "",
                    "dashboardAlerts": False,
                    "groupBy": [],
                    "groupMode": "default",
                    "maxItems": 20,
                    "sortOrder": 1,
                    "stateFilter": {
                        "firing": True,
                        "pending": True,
                        "noData": False,
                        "normal": False,
                        "error": True,
                    },
                    "viewMode": "list",
                }
            },
            ds=None,
        )
    )
    y += 4
    if aws:
        P.append(row("AWS resources (CloudWatch)", y))
        y += 1
        P += [
            panel(
                "timeseries",
                "Job queue depth and dead letters",
                [
                    cw(
                        "AWS/SQS",
                        "ApproximateNumberOfMessagesVisible",
                        {"QueueName": ["*"]},
                        "Maximum",
                        label="{{QueueName}}",
                    )
                ],
                0,
                y,
                8,
                7,
                ds=CW,
                thr=thresholds(5, 20),
            ),
            panel(
                "timeseries",
                "ECS CPU by service",
                [
                    cw(
                        "AWS/ECS",
                        "CPUUtilization",
                        {"ClusterName": ["*"], "ServiceName": ["*"]},
                        "Average",
                        label="{{ServiceName}}",
                    )
                ],
                8,
                y,
                8,
                7,
                unit="percent",
                ds=CW,
                extra=rename_series(ECS_SERVICE_RENAME),
            ),
            panel(
                "timeseries",
                "ECS memory by service",
                [
                    cw(
                        "AWS/ECS",
                        "MemoryUtilization",
                        {"ClusterName": ["*"], "ServiceName": ["*"]},
                        "Average",
                        label="{{ServiceName}}",
                    )
                ],
                16,
                y,
                8,
                7,
                unit="percent",
                ds=CW,
                extra=rename_series(ECS_SERVICE_RENAME),
            ),
        ]
        y += 7
        P += [
            panel(
                "timeseries",
                "Bedrock throttles and client errors",
                [
                    cw(
                        "AWS/Bedrock",
                        "InvocationThrottles",
                        {"ModelId": ["*"]},
                        "Sum",
                        label="throttle {{ModelId}}",
                    ),
                    cw(
                        "AWS/Bedrock",
                        "InvocationClientErrors",
                        {"ModelId": ["*"]},
                        "Sum",
                        label="client error {{ModelId}}",
                    ),
                ],
                0,
                y,
                12,
                7,
                ds=CW,
                thr=thresholds(1, 5),
            ),
            panel(
                "timeseries",
                "Aurora capacity (ACU)",
                [
                    cw(
                        "AWS/RDS",
                        "ServerlessDatabaseCapacity",
                        {"DBClusterIdentifier": ["*"]},
                        "Average",
                        label="{{DBClusterIdentifier}}",
                    )
                ],
                12,
                y,
                12,
                7,
                ds=CW,
            ),
        ]
        y += 7
    return dashboard(
        "argus-operator",
        "Argus · Operations",
        ["operator"],
        P,
        links=persona_links(),
        annotations=deploy_annotations(),
    )


# ------------------------------------------------------------------------------ analyst
def analyst(aws: bool):
    global _id
    _id = 0
    P = []
    y = 0
    variables = [
        {
            "name": "investigation",
            "type": "textbox",
            "label": "Investigation id",
            "query": "",
            "current": {"text": "", "value": ""},
            "hide": 0,
        },
        {
            "name": "mmsi",
            "type": "textbox",
            "label": "MMSI",
            "query": "",
            "current": {"text": "", "value": ""},
            "hide": 0,
        },
        {
            "name": "region",
            "type": "textbox",
            "label": "AWS region",
            "query": "us-east-1",
            "current": {
                "text": "us-east-1",
                "value": "us-east-1",
            },
            "hide": 0,
        },
    ]
    P.append(
        panel(
            "text",
            "How to read this board",
            [],
            0,
            y,
            24,
            3,
            extra={
                "options": {
                    "mode": "markdown",
                    "content": "Paste an **investigation id** (from the watch floor's report panel) to see its trace, its node timeline and what it cost. The **CloudWatch GenAI Observability session** link opens the same investigation's prompts, completions and the officer's verdict. Traces come from Tempo, logs from Loki, tokens from the provenance manifest.",
                }
            },
            ds=None,
        )
    )
    y += 3
    P.append(row("This investigation", y))
    y += 1
    P += [
        panel(
            "traces",
            "Traces for $investigation",
            [
                traceql(
                    '{ span.session.id = "$investigation" || resource.service.name = "agent-orchestrator" && span.session.id = "$investigation" }'
                )
            ],
            0,
            y,
            24,
            8,
            ds=TEMPO,
            extra={
                "links": [
                    {
                        "title": "Open in CloudWatch GenAI Observability",
                        "url": "https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}#gen-ai-observability/agent-core",
                        "targetBlank": True,
                    }
                ]
            },
        ),
    ]
    y += 8
    P += [
        panel(
            "logs",
            "Node timeline (orchestrator and specialists)",
            [loki('{service_name=~"agent-.*"} |= "$investigation"')],
            0,
            y,
            16,
            10,
            ds=LOKI,
            extra={
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "sortOrder": "Ascending",
                    "dedupStrategy": "none",
                }
            },
        ),
        panel(
            "bargauge",
            "Tokens by node (24 h, all investigations)",
            [
                prom_instant(
                    "sum by (node, kind) (argus_node_tokens_24h)", "{{node}} {{kind}}"
                )
            ],
            16,
            y,
            8,
            10,
            unit="short",
        ),
    ]
    y += 10
    P.append(row("Tools and models behind the case", y))
    y += 1
    P += [
        panel(
            "timeseries",
            "MCP tool calls by tool",
            [
                prom(
                    'sum by (mcp_tool_name) (rate(traces_spanmetrics_calls_total{mcp_tool_name!=""}[5m]))',
                    "{{mcp_tool_name}}",
                )
            ],
            0,
            y,
            8,
            7,
            unit="reqps",
        ),
        panel(
            "timeseries",
            "Model calls by model",
            [
                prom(
                    'sum by (gen_ai_request_model) (rate(traces_spanmetrics_calls_total{gen_ai_request_model!=""}[5m]))',
                    "{{gen_ai_request_model}}",
                )
            ],
            8,
            y,
            8,
            7,
            unit="reqps",
        ),
        panel(
            "timeseries",
            "Tool p95 latency",
            [
                prom(
                    'histogram_quantile(0.95, sum by (le, mcp_tool_name) (rate(traces_spanmetrics_latency_bucket{mcp_tool_name!=""}[5m])))',
                    "{{mcp_tool_name}}",
                )
            ],
            16,
            y,
            8,
            7,
            unit="ms",
        ),
    ]
    y += 7
    P.append(
        panel(
            "logs",
            "Everything the tools and agents logged for MMSI $mmsi",
            [loki('{service_name=~"agent-.*|mcp-.*"} |= "$mmsi"')],
            0,
            y,
            24,
            8,
            ds=LOKI,
            extra={
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "sortOrder": "Descending",
                }
            },
        )
    )
    y += 8
    return dashboard(
        "argus-analyst",
        "Argus · Investigation",
        ["analyst"],
        P,
        variables=variables,
        links=persona_links(),
        refresh="",
    )


# ------------------------------------------------------------------------------ model owner
def model_owner(aws: bool):
    global _id
    _id = 0
    P = []
    y = 0
    P.append(row("Evaluation gates", y))
    y += 1
    P += [
        panel(
            "stat",
            "Latest eval recall",
            [prom_instant("argus_eval_recall_latest")],
            0,
            y,
            4,
            4,
            unit="percentunit",
            thr=thresholds(*T["eval_recall"], invert=True),
        ),
        panel(
            "stat",
            "Suites passing",
            [
                prom_instant("sum(argus_eval_passed)"),
                prom_instant("count(argus_eval_passed)"),
            ],
            4,
            y,
            4,
            4,
            desc="Passing suites out of suites with a run.",
        ),
        panel(
            "timeseries",
            "Eval scores by suite and metric",
            [prom("argus_eval_score", "{{suite}} {{metric}}")],
            8,
            y,
            16,
            8,
            unit="percentunit",
            thr=thresholds(*T["eval_recall"], invert=True),
        ),
    ]
    y += 8
    P.append(row("Prompt and model behaviour", y))
    y += 1
    P += [
        panel(
            "timeseries",
            "Model calls by model",
            [
                prom(
                    'sum by (gen_ai_request_model) (rate(traces_spanmetrics_calls_total{gen_ai_request_model!=""}[5m]))',
                    "{{gen_ai_request_model}}",
                )
            ],
            0,
            y,
            8,
            7,
            unit="reqps",
        ),
        panel(
            "timeseries",
            "Model p95 latency",
            [
                prom(
                    'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate(traces_spanmetrics_latency_bucket{gen_ai_request_model!=""}[5m])))',
                    "{{gen_ai_request_model}}",
                )
            ],
            8,
            y,
            8,
            7,
            unit="ms",
        ),
        panel(
            "bargauge",
            "Tokens by node and model (24 h)",
            [
                prom_instant(
                    "sum by (node, model, kind) (argus_node_tokens_24h)",
                    "{{node}} · {{model}} · {{kind}}",
                )
            ],
            16,
            y,
            8,
            7,
            unit="short",
        ),
    ]
    y += 7
    P += [
        panel(
            "stat",
            "Rejections (24 h)",
            [prom_instant("argus_node_rejections_24h")],
            0,
            y,
            4,
            4,
            thr=thresholds(3, 10),
            desc="Policy or schema rejections: a prompt regression shows here first.",
        ),
        panel(
            "stat",
            "Escalations (24 h)",
            [prom_instant("argus_node_escalations_24h")],
            4,
            y,
            4,
            4,
            thr=thresholds(5, 15),
            desc="Nodes that needed the stronger tier.",
        ),
        panel(
            "stat",
            "Nodes run (24 h)",
            [prom_instant("argus_nodes_run_24h")],
            8,
            y,
            4,
            4,
        ),
        panel(
            "timeseries",
            "Cost per investigation",
            [
                prom("argus_investigation_cost_usd_avg", "30-day average"),
                prom("argus_target_investigation_cost_usd_avg", "budget"),
            ],
            12,
            y,
            12,
            6,
            unit="currencyUSD",
            thr=thresholds(1.0, 2.0),
        ),
    ]
    y += 6
    P.append(
        panel(
            "text",
            "Change control",
            [],
            0,
            y,
            24,
            3,
            extra={
                "options": {
                    "mode": "markdown",
                    "content": "A prompt or model change is not done until `evals/node_evals.py --gate` passes (ADR-0009). Every investigation's manifest records the prompt hash and model per node; compare a failing run's hashes with the last passing one. Prompts are versioned in Bedrock Prompt Management; the eval cases become an AgentCore Evaluations dataset with `evals/agentcore_dataset.py`.",
                }
            },
            ds=None,
        )
    )
    y += 3
    if aws:
        P.append(row("Bedrock (CloudWatch)", y))
        y += 1
        P += [
            panel(
                "timeseries",
                "Invocations by model",
                [
                    cw(
                        "AWS/Bedrock",
                        "Invocations",
                        {"ModelId": ["*"]},
                        "Sum",
                        label="{{ModelId}}",
                    )
                ],
                0,
                y,
                8,
                7,
                ds=CW,
            ),
            panel(
                "timeseries",
                "Throttles and errors",
                [
                    cw(
                        "AWS/Bedrock",
                        "InvocationThrottles",
                        {"ModelId": ["*"]},
                        "Sum",
                        label="throttle {{ModelId}}",
                    ),
                    cw(
                        "AWS/Bedrock",
                        "InvocationServerErrors",
                        {"ModelId": ["*"]},
                        "Sum",
                        label="server error {{ModelId}}",
                    ),
                ],
                8,
                y,
                8,
                7,
                ds=CW,
                thr=thresholds(1, 5),
            ),
            panel(
                "timeseries",
                "Guardrail interventions",
                [
                    cw(
                        "AWS/Bedrock/Guardrails",
                        "InvocationsIntervened",
                        {"GuardrailId": ["*"]},
                        "Sum",
                        label="{{GuardrailId}}",
                    )
                ],
                16,
                y,
                8,
                7,
                ds=CW,
                desc="Prompts or outputs the argus-agents guardrail blocked.",
            ),
        ]
        y += 7
    return dashboard(
        "argus-model-owner",
        "Argus · Models and evals",
        ["model-owner"],
        P,
        links=persona_links(),
        annotations=deploy_annotations(),
    )


# ------------------------------------------------------------------------------ watch floor
def watch_floor(aws: bool):
    global _id
    _id = 0
    P = []
    y = 0
    P += [
        panel(
            "stat",
            "Feed",
            [prom_instant("argus_feed_lag_s")],
            0,
            y,
            4,
            4,
            unit="s",
            thr=thresholds(*T["feed_lag_s"]),
            desc="Seconds since the newest position; red means the picture is stale.",
        ),
        panel(
            "stat",
            "Vessels reporting",
            [prom_instant("argus_vessels_reporting_10m")],
            4,
            y,
            4,
            4,
            thr=thresholds(1, 0, invert=True),
        ),
        panel(
            "stat",
            "Needs a decision",
            [prom_instant("argus_alerts_awaiting_review + argus_tasking_proposed")],
            8,
            y,
            4,
            4,
            thr=thresholds(5, 15),
            desc="Draft alerts plus proposed tasking requests.",
        ),
        panel(
            "stat",
            "Oldest unreviewed",
            [prom_instant("argus_oldest_unreviewed_alert_s / 60")],
            12,
            y,
            4,
            4,
            unit="m",
            thr=thresholds(*T["oldest_unreviewed_min"]),
        ),
        panel(
            "stat",
            "Cases in progress",
            [prom_instant('argus_investigations_by_status{status="running"}')],
            16,
            y,
            4,
            4,
        ),
        panel(
            "stat",
            "Tasking awaiting approval",
            [prom_instant("argus_tasking_proposed")],
            20,
            y,
            4,
            4,
            thr=thresholds(1, 5),
        ),
    ]
    y += 4
    P += [
        panel(
            "bargauge",
            "Open alerts by severity",
            [prom_instant("argus_alerts_open_by_severity", "{{severity}}")],
            0,
            y,
            8,
            7,
            thr=thresholds(5, 15),
        ),
        panel(
            "timeseries",
            "Backlog over time",
            [
                prom("argus_alerts_awaiting_review", "alerts awaiting review"),
                prom("argus_tasking_proposed", "tasking proposed"),
            ],
            8,
            y,
            16,
            7,
        ),
    ]
    y += 7
    P.append(
        panel(
            "logs",
            "What the Watch agent raised (latest first)",
            [loki('{service_name="agent-watch"} |= "raise_alert"')],
            0,
            y,
            24,
            8,
            ds=LOKI,
            extra={
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "sortOrder": "Descending",
                }
            },
        )
    )
    y += 8
    P.append(
        panel(
            "text",
            "Where to act",
            [],
            0,
            y,
            24,
            3,
            extra={
                "options": {
                    "mode": "markdown",
                    "content": "Decisions are made on the **watch floor**, not here: this board shows whether the picture is alive and how much is waiting. Accept or reject alerts and reports, and approve or reject tasking, in the Argus UI with your officer id set.",
                }
            },
            ds=None,
        )
    )
    y += 3
    return dashboard(
        "argus-watch-floor",
        "Argus · Watch floor",
        ["watch-floor"],
        P,
        links=persona_links(),
        refresh="15s",
    )


# ------------------------------------------------------------------ the one board
# The four persona builders above are the panel library: every query is defined once
# there. The board people open is this one: a KPI strip, the watch floor open, and the
# deeper sections collapsed so the page answers "what needs me now" first and "why" on
# demand. Sections are ordered by how often someone needs them.

SECTIONS = [
    (
        "Watch floor: what needs a decision now",
        False,
        [
            ("Open alerts by severity", 8, 8),
            ("Backlog over time", 8, 8),
            ("What the Watch agent raised (latest first)", 8, 8),
        ],
    ),
    (
        "Pipeline health and service levels",
        True,
        [
            ("Sweep latency p95", 8, 7),
            ("Investigation latency p95", 8, 7),
            ("Alert to investigation lag p95", 8, 7),
            ("Investigation completion", 6, 6),
            ("Error budget burn", 6, 6),
            ("Jobs in the last 24 h", 12, 6),
            ("Job queue depth and dead letters", 12, 7),
            ("Errors in agent logs (5 m)", 12, 7),
            ("Firing alerts", 24, 6),
        ],
    ),
    (
        "Agents and tools",
        True,
        [
            ("MCP tool calls by tool", 8, 7),
            ("Tool p95 latency", 8, 7),
            ("Model calls by model", 8, 7),
            ("Span rate by service", 12, 7),
            ("p95 latency by service", 12, 7),
            ("Service graph", 24, 9),
        ],
    ),
    (
        "One investigation (pick an investigation id and MMSI above)",
        True,
        [
            ("How to read this board", 24, 4),
            ("Traces for $investigation", 12, 9),
            ("Node timeline (orchestrator and specialists)", 12, 9),
            ("Everything the tools and agents logged for MMSI $mmsi", 24, 10),
        ],
    ),
    (
        "Models, cost and quality",
        True,
        [
            ("Tokens by node and model (24 h)", 12, 8),
            ("Cost per investigation", 12, 8),
            ("Rejections (24 h)", 4, 4),
            ("Escalations (24 h)", 4, 4),
            ("Nodes run (24 h)", 4, 4),
            ("Suites passing", 4, 4),
            ("Latest eval recall", 4, 4),
            ("Eval scores by suite and metric", 20, 8),
            ("Change control", 4, 8),
        ],
    ),
]

AWS_SECTION = (
    "AgentCore, Bedrock and AWS resources",
    True,
    [
        ("Online evaluation scores", 12, 8),
        ("Agent and tool-server invocations", 12, 8),
        ("Gateway tool calls", 12, 8),
        ("Memory operations", 12, 8),
        ("Agent and tool-server errors", 8, 7),
        ("Guardrail interventions", 8, 7),
        ("Bedrock throttles and client errors", 8, 7),
        ("ECS CPU by service", 8, 7),
        ("ECS memory by service", 8, 7),
        ("Aurora capacity (ACU)", 8, 7),
    ],
)


def _agentcore_panels():
    """Panels that only exist on AWS and were not in the persona library."""
    ns = "AWS/Bedrock-AgentCore"
    return {
        "Online evaluation scores": panel(
            "timeseries",
            "Online evaluation scores",
            [
                {
                    "queryMode": "Metrics",
                    "region": "default",
                    "metricQueryType": 0,
                    "metricEditorMode": 1,
                    "expression": "SEARCH('{Bedrock-AgentCore/Evaluations}', 'Average', 300)",
                    "label": "${PROP('MetricName')} ${PROP('Dim.EvaluatorName')}",
                    "period": "300",
                    "statistic": "Average",
                    "namespace": "Bedrock-AgentCore/Evaluations",
                    "metricName": "",
                    "dimensions": {},
                    "matchExact": False,
                }
            ],
            0,
            0,
            12,
            8,
            desc="AgentCore Evaluations on live sessions: built-in helpfulness, correctness, tool selection and parameters, goal success, harmfulness, plus the Argus report rubric (1 to 5).",
            ds=CW,
        ),
        "Agent and tool-server invocations": panel(
            "timeseries",
            "Agent and tool-server invocations",
            [
                cw_math(
                    "SUM(SEARCH('{" + ns + ",Name,Operation,Resource} "
                    f'MetricName="Invocations" Name="{name}::DEFAULT"\', \'Sum\', 300))',
                    label,
                )
                for name, label in AGENTCORE_RUNTIMES
            ],
            0,
            0,
            12,
            8,
            desc="InvokeAgentRuntime calls per AgentCore runtime: the four agents and the four MCP tool servers, summed over runtime ids so a redeploy does not split a line.",
            ds=CW,
        ),
        "Gateway tool calls": panel(
            "timeseries",
            "Gateway tool calls",
            [
                cw(
                    ns,
                    "Invocations",
                    {
                        "Operation": ["InvokeGateway"],
                        "Name": ["*"],
                        "Method": ["*"],
                        "Protocol": ["*"],
                    },
                    "Sum",
                    "300",
                    "{{Name}}",
                    exact=True,
                )
            ],
            0,
            0,
            12,
            8,
            desc="Calls through the two AgentCore gateways: each MCP tool as server.tool (the tools gateway) and the A2A agent calls (the agents gateway).",
            ds=CW,
            extra=rename_series(
                (r"^(\w+?)___(\w+)$", "$1.$2"),
                (r"^InvokeHttp$", "agents gateway · A2A"),
            ),
        ),
        "Memory operations": panel(
            "timeseries",
            "Memory operations",
            [
                cw(
                    ns,
                    "Invocations",
                    {"Operation": ["*"]},
                    "Sum",
                    "300",
                    "{{Operation}}",
                    exact=True,
                )
            ],
            0,
            0,
            8,
            7,
            desc="AgentCore Memory: events written and read, records retrieved, and the extraction and consolidation runs behind long-term recall.",
            ds=CW,
        ),
        "Agent and tool-server errors": panel(
            "timeseries",
            "Agent and tool-server errors",
            [
                cw_math(
                    "SUM(SEARCH('{" + ns + ",Name,Operation,Resource} "
                    'MetricName=("SystemErrors" OR "UserErrors") '
                    f"Name=\"{name}::DEFAULT\"', 'Sum', 300))",
                    label,
                )
                for name, label in AGENTCORE_RUNTIMES
            ],
            0,
            0,
            8,
            7,
            desc="System and user errors per runtime as AgentCore counts them (a 4xx from the runtime is a user error, a failed start or timeout a system error).",
            ds=CW,
        ),
    }


def _library(aws: bool) -> dict:
    """Every persona panel by title, ignoring panels that read the retired CloudWatch
    metrics fan-out (Argus/Agents lives in Prometheus now)."""
    lib: dict = {}
    for build in (watch_floor, operator, analyst, model_owner):
        d = build(aws)
        for p in d["panels"]:
            if p["type"] == "row":
                for q in p.get("panels", []):
                    lib.setdefault(q["title"], q)
                continue
            if "Argus/Agents" in json.dumps(p):
                continue
            lib.setdefault(p["title"], p)
        lib.setdefault("__variables__", d["templating"]["list"])
        if d["templating"]["list"]:
            lib["__variables__"] = d["templating"]["list"]
    if aws:
        lib.update(_agentcore_panels())
    return lib


def _place(specs, lib, y):
    """Lay panels out left to right on the 24-column grid; returns (panels, next y)."""
    global _id
    out, x, row_h = [], 0, 0
    for title, w, h in specs:
        p = lib.get(title)
        if p is None:
            continue
        if x + w > 24:
            x, y = 0, y + row_h
            row_h = 0
        _id += 1
        q = dict(p, id=_id, gridPos={"x": x, "y": y, "w": w, "h": h})
        out.append(q)
        x += w
        row_h = max(row_h, h)
    return out, y + row_h


def consolidated(aws: bool):
    global _id
    _id = 0
    lib = _library(aws)
    kpis = [
        ("Feed", 3, 4),
        ("Vessels reporting", 3, 4),
        ("Needs a decision", 3, 4),
        ("Oldest unreviewed", 3, 4),
        ("Cases in progress", 3, 4),
        ("Tasking awaiting approval", 3, 4),
        ("Cost today", 3, 4),
        ("Latest eval recall", 3, 4),
    ]
    panels, y = _place(kpis, lib, 0)
    sections = list(SECTIONS) + ([AWS_SECTION] if aws else [])
    for title, collapsed, specs in sections:
        r = row(title, y)
        r["collapsed"] = collapsed
        y += 1
        inner, y_next = _place(specs, lib, y)
        if collapsed:
            r["panels"] = inner
            panels.append(r)
            y += 0
        else:
            panels.append(r)
            panels.extend(inner)
            y = y_next
    links = [
        {
            "title": "Watch floor (UI)",
            "type": "link",
            "url": "/",
            "icon": "external link",
        },
        {
            "title": "AgentCore sessions (CloudWatch)",
            "type": "link",
            "url": "https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}#gen-ai-observability/agent-core",
            "icon": "external link",
            "targetBlank": True,
        },
    ]
    return dashboard(
        "argus",
        "Argus",
        ["consolidated"],
        panels,
        variables=lib.get("__variables__", []),
        links=links,
        refresh="30s",
        annotations=deploy_annotations(),
    )


def main() -> None:
    for out, aws in ((LOCAL, False), (AWS, True)):
        out.mkdir(parents=True, exist_ok=True)
        for old in (
            "agents.json",
            "slo.json",
            "argus-operator.json",
            "argus-analyst.json",
            "argus-model-owner.json",
            "argus-watch-floor.json",
        ):
            (out / old).unlink(missing_ok=True)
        d = consolidated(aws)
        n = sum(1 + len(p.get("panels", [])) for p in d["panels"])
        (out / "argus.json").write_text(json.dumps(d, indent=1) + "\n")
        print(f"wrote {out / 'argus.json'} ({n} panels incl. rows)")


if __name__ == "__main__":
    main()
