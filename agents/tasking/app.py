"""Tasking Agent: Strands agent over A2A. Decides whether imagery collection would help and proposes it."""

from __future__ import annotations

import logging

from a2a.types import AgentSkill
from shared.config import settings
from shared.mcp_tools import wait_for_mcp
from shared.models import for_tier, strands_model
from shared.prompts_loader import load_prompt, prompt_hash, prompt_version
from shared.schemas import TaskingRecommendation
from shared.telemetry import configure_strands
from shared.tools import health_urls, strands_tool_clients
from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
configure_strands("agent-tasking")


def build_agent() -> Agent:
    return Agent(
        name="Tasking Agent",
        description="Checks the imagery archive, estimates the next satellite pass and proposes collection requests for human approval.",
        model=strands_model(role="tasking"),
        conversation_manager=SlidingWindowConversationManager(
            window_size=40, should_truncate_results=True
        ),
        system_prompt=load_prompt(
            "tasking",
            scenario_end=settings.scenario_end,
            schema=TaskingRecommendation.model_json_schema(),
        ),
        tools=strands_tool_clients(["imagery", "geo"]),
        trace_attributes={
            "agent.role": "tasking",
            "deployment.environment": settings.deploy_env,
        },
    )


skills = [
    AgentSkill(
        id="collection-plan",
        name="Collection planning",
        description="Given a vessel, an evidence gap position and time window, search archives and propose a re-look.",
        tags=["argus", "imagery", "tasking"],
    )
]
wait_for_mcp(health_urls())
server = A2AServer(
    agent=build_agent(),
    host="0.0.0.0",
    port=settings.a2a_port,
    http_url=settings.a2a_public_url or None,
    skills=skills,
    version="0.1.0",
)
app = server.to_fastapi_app()


@app.get("/ping")
def ping():
    return {"status": "Healthy"}


@app.get("/provenance")
def provenance():
    s, tier = for_tier("tasking")
    return {
        "provider": s.model_provider,
        "model_id": s.model_id,
        "tier": tier,
        "prompt": prompt_hash("tasking"),
        "prompt_version": prompt_version("tasking"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.a2a_port, log_level="info")
