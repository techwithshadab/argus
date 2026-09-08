"""The agents' A2A cards, as data. The apps build their AgentCard from these and the CDK
deploy publishes the same cards to AWS Agent Registry, so the catalog and the running
agents never disagree about what an agent does."""

from __future__ import annotations

VERSION = "0.2.0"

CARDS: dict[str, dict] = {
    "watch": {
        "name": "Watch Agent",
        "description": "Sweeps AIS traffic for a time window and raises evidenced anomaly alerts (AIS gaps, MMSI spoofing, loitering, rendezvous, zone incursions). Detector candidates are built in code; the agent judges each one.",
        "protocol": "A2A",
        "skills": [
            {
                "id": "sweep",
                "name": "Anomaly sweep",
                "description": "Review AIS for a window and raise alerts. Input: hours to look back, optional focus MMSI.",
                "tags": ["argus", "ais", "anomaly"],
                "examples": ["Sweep the last 12 hours and raise alerts."],
            }
        ],
    },
    "investigator": {
        "name": "Investigator Agent",
        "description": "Builds an identity, ownership, sanctions and behaviour case file for one vessel (by MMSI) from AIS, registry and geo tools. Returns JSON findings.",
        "protocol": "A2A",
        "skills": [
            {
                "id": "investigate",
                "name": "Vessel investigation",
                "description": "Input: MMSI and triggering alert. Output: InvestigationFindings JSON.",
                "tags": ["argus", "investigation", "sanctions"],
            }
        ],
    },
    "tasking": {
        "name": "Tasking Agent",
        "description": "Decides whether imagery collection would reduce uncertainty about a vessel and proposes a tasking request (archive search, next-pass estimate). Proposals only; a watch officer approves.",
        "protocol": "A2A",
        "skills": [
            {
                "id": "tasking",
                "name": "Collection tasking",
                "description": "Input: vessel, behaviour summary and evidence gap. Output: TaskingRecommendation JSON.",
                "tags": ["argus", "imagery", "tasking"],
            }
        ],
    },
    "orchestrator": {
        "name": "Orchestrator",
        "description": "Code-owned investigation graph: recalls memory, runs the two Investigator branches in parallel, merges, asks Tasking, writes the report on the strong tier, checks it against policy and persists it with a provenance manifest.",
        "protocol": "HTTP",
        "skills": [
            {
                "id": "investigate",
                "name": "Run an investigation",
                "description": "Input: MMSI, trigger, triggering alert, investigation id. Output: Vessel of Interest report and manifest.",
                "tags": ["argus", "orchestration"],
            }
        ],
    },
}


def a2a_card(name: str, url: str) -> dict:
    """An A2A agent card (v0.3 shape) for the registry or the agent's own /.well-known."""
    c = CARDS[name]
    return {
        "name": c["name"],
        "description": c["description"],
        "version": VERSION,
        "protocolVersion": "0.3.0",
        "url": url,
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                k: v
                for k, v in s.items()
                if k in ("id", "name", "description", "tags", "examples")
            }
            for s in c["skills"]
        ],
    }
