"""Pure aggregation over provenance manifests for the SLIs (no database, unit-tested)."""

from __future__ import annotations


def node_stats(manifests: list[dict]) -> dict:
    """Tokens per node and model, escalations (a node needing more than one attempt) and
    policy or schema rejections, across the given manifests."""
    tokens: dict[tuple[str, str, str], int] = {}
    escalations = 0
    rejections = 0
    nodes = 0
    for m in manifests:
        for n in (m or {}).get("nodes") or []:
            nodes += 1
            node = str(n.get("node") or "unknown")
            model = str(n.get("model_id") or "none")
            for kind, key in (("input", "inputTokens"), ("output", "outputTokens")):
                v = (n.get("usage") or {}).get(key) or 0
                if v:
                    tokens[(node, model, kind)] = tokens.get(
                        (node, model, kind), 0
                    ) + int(v)
            if int(n.get("attempts") or 1) > 1:
                escalations += 1
            if n.get("policy_problems") or n.get("schema_problems"):
                rejections += 1
    return {
        "tokens": tokens,
        "escalations": escalations,
        "rejections": rejections,
        "nodes": nodes,
    }


def prometheus_lines(stats: dict) -> list[str]:
    lines = ["# TYPE argus_node_tokens_24h gauge"]
    for (node, model, kind), v in sorted(stats["tokens"].items()):
        lines.append(
            f'argus_node_tokens_24h{{node="{node}",model="{model}",kind="{kind}"}} {v}'
        )
    lines.append(f"argus_node_escalations_24h {stats['escalations']}")
    lines.append(f"argus_node_rejections_24h {stats['rejections']}")
    lines.append(f"argus_nodes_run_24h {stats['nodes']}")
    return lines
