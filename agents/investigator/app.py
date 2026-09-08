"""Investigator Agent: LangGraph ReAct agent, tools from three MCP servers, served over A2A.

Deliberately a different framework from the Strands agents to show that A2A and MCP make the
framework choice a per-agent decision rather than a platform decision."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from shared.config import next_tier, settings
from shared.graph import findings_payload, guardrail_blocked, json_object
from shared.models import for_tier, langchain_model, model_unavailable
from shared.prompts_loader import load_prompt, prompt_hash, prompt_version
from shared.schemas import InvestigationFindings
from shared.telemetry import configure_langchain
from shared.tools import langchain_connections, split_tool_name
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-investigator")
configure_langchain("agent-investigator")

SYSTEM = load_prompt(
    "investigator",
    scenario_end=settings.scenario_end,
    schema=json.dumps(InvestigationFindings.model_json_schema()),
)


ROLE = "investigator"


def provenance(tier: str, attempts: int = 1) -> dict:
    s, t = for_tier(ROLE, tier)
    return {
        "provider": s.model_provider,
        "model_id": s.model_id,
        "tier": t,
        "prompt": prompt_hash(ROLE),
        "prompt_version": prompt_version(ROLE),
        "attempts": attempts,
    }


class InvestigatorGraph:
    """Builds one LangGraph agent per model tier, lazily (MCP tool discovery is async)."""

    def __init__(self):
        self._graphs: dict[str, object] = {}
        self._tools = None
        self._tool_servers: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def graph(self, tier: str | None = None):
        _, tier = for_tier(ROLE, tier)
        async with self._lock:
            if tier not in self._graphs:
                if self._tools is None:
                    connections = langchain_connections(["ais", "registry", "geo"])
                    client = MultiServerMCPClient(connections)
                    self._tools = await client.get_tools()
                    for server in connections:
                        for t in await client.get_tools(server_name=server):
                            srv, _ = split_tool_name(t.name)
                            self._tool_servers[t.name] = srv or server
                    log.info("loaded %d MCP tools", len(self._tools))
                llm = langchain_model(role=ROLE, tier=tier)
                self._graphs[tier] = create_react_agent(
                    llm, self._tools, prompt=SYSTEM, name="investigator"
                )
            return self._graphs[tier], tier

    async def run(self, user_text: str, context_id: str | None) -> str:
        """Run on the role's tier; if the output fails validation, escalate one tier and retry once."""
        _, tier = for_tier(ROLE)
        attempts = 0
        mmsi, scope = parse_request(user_text)
        while True:
            attempts += 1
            try:
                out = await self._run_once(user_text, context_id, tier, mmsi, scope)
            except Exception as e:  # noqa: BLE001
                up = next_tier(tier)
                if up is None or attempts >= 2 or not model_unavailable(e):
                    raise
                # Throttled or unavailable after the SDK's own retries: one step up (another
                # model, its own quota) before the orchestrator degrades the branch.
                log.warning(
                    "investigator tier %s unavailable (%s); escalating to %s",
                    tier,
                    str(e)[:120],
                    up,
                )
                tier = up
                continue
            if not out.startswith('{"error"'):
                data = json.loads(out)
                data["provenance"] = {
                    **provenance(tier, attempts),
                    "usage": getattr(self, "_last_usage", {}),
                }
                return json.dumps(data)
            up = next_tier(tier)
            if up is None or attempts >= 2:
                return out
            log.warning(
                "investigator output invalid on tier %s; escalating to %s", tier, up
            )
            tier = up

    async def _run_once(
        self,
        user_text: str,
        context_id: str | None,
        tier: str,
        mmsi: int | None = None,
        scope: str = "full",
    ) -> str:
        g, _ = await self.graph(tier)
        result = await g.ainvoke(
            {"messages": [{"role": "user", "content": user_text}]},
            config={
                "recursion_limit": 60,
                "metadata": {
                    "a2a.context_id": context_id or "",
                    "agent.role": "investigator",
                    "session.id": parse_session(user_text),
                },
            },
        )
        text = _last_text(result)
        if not guardrail_blocked(text):
            try:
                json_object(text)
            except ValueError:
                # Prose instead of JSON: one cheap follow-up turn on the same conversation
                # before the executor escalates to a stronger tier.
                log.warning("investigator answered without JSON; asking for the object")
                result = await g.ainvoke(
                    {
                        "messages": result["messages"]
                        + [
                            {
                                "role": "user",
                                "content": "Return only the InvestigationFindings JSON "
                                "object for those findings, no prose.",
                            }
                        ]
                    },
                    config={"recursion_limit": 10},
                )
                text = _last_text(result)
        usage = {"inputTokens": 0, "outputTokens": 0}
        for m in result["messages"]:
            u = getattr(m, "usage_metadata", None) or {}
            usage["inputTokens"] += int(u.get("input_tokens", 0) or 0)
            usage["outputTokens"] += int(u.get("output_tokens", 0) or 0)
        self._last_usage = usage
        return _validate(text, mmsi, scope, self._tool_servers)


def _last_text(result: dict) -> str:
    content = result["messages"][-1].content
    return (
        content
        if isinstance(content, str)
        else "".join(c.get("text", "") for c in content if isinstance(c, dict))
    )


def parse_request(text: str) -> tuple[int | None, str]:
    """The vessel id and scope the orchestrator put in the request, if any."""
    m = re.search(r"Vessel MMSI:\s*(\d+)", text)
    s = re.search(r"Scope:\s*([a-z]+)", text, re.I)
    return (int(m.group(1)) if m else None), (s.group(1).lower() if s else "full")


def parse_session(text: str) -> str:
    """The investigation id the orchestrator names in the request; the trace session."""
    m = re.search(r"Investigation:\s*(\S+)", text)
    return m.group(1) if m else "adhoc"


def _validate(
    text: str,
    mmsi: int | None = None,
    scope: str = "full",
    tool_servers: dict | None = None,
) -> str:
    """Parse the model's JSON, coerce the shapes models get wrong, validate against the
    schema and return canonical JSON. If validation still fails, return the raw text wrapped
    with an error so the orchestrator can see it."""
    if guardrail_blocked(text):
        return json.dumps({"error": "guardrail blocked the request", "raw": text[:400]})
    try:
        raw = json_object(text)
    except ValueError:
        log.warning("investigator answer had no JSON: %s", text[:200])
        return json.dumps({"error": "no JSON in response", "raw": text[:4000]})
    try:
        payload = findings_payload(raw, mmsi, scope, tool_servers)
        return InvestigationFindings.model_validate(payload).model_dump_json()
    except Exception as e:  # noqa: BLE001
        log.warning("investigator answer failed the schema: %s", str(e)[:300])
        return json.dumps(
            {"error": f"schema validation failed: {e}", "raw": json.dumps(raw)[:4000]}
        )


class InvestigatorExecutor(AgentExecutor):
    def __init__(self):
        self.agent = InvestigatorGraph()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = context.get_user_input()
        log.info("investigation request: %s", text[:200])
        try:
            answer = await self.agent.run(text, context.context_id)
        except Exception as e:  # noqa: BLE001
            # A failed branch is data for the orchestrator (degraded), not a transport error.
            log.exception("investigation failed")
            answer = json.dumps({"error": f"investigator failed: {e}"[:400]})
        await event_queue.enqueue_event(
            new_agent_text_message(answer, context.context_id, context.task_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


card = AgentCard(
    name="Investigator Agent",
    description="Builds an identity, ownership, sanctions and behaviour case file for one vessel (by MMSI) from AIS, registry and geo tools. Returns JSON findings.",
    url=settings.a2a_public_url or f"http://0.0.0.0:{settings.a2a_port}/",
    version="0.1.0",
    capabilities=AgentCapabilities(streaming=False),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="investigate",
            name="Vessel investigation",
            description="Input: MMSI and triggering alert. Output: InvestigationFindings JSON.",
            tags=["argus", "investigation", "sanctions"],
        )
    ],
)

handler = DefaultRequestHandler(
    agent_executor=InvestigatorExecutor(), task_store=InMemoryTaskStore()
)
app = A2AStarletteApplication(agent_card=card, http_handler=handler).build()


async def ping(_):
    return JSONResponse({"status": "Healthy"})


async def provenance_route(_):
    return JSONResponse(provenance(None))


app.router.routes.insert(0, Route("/ping", ping))
app.router.routes.insert(0, Route("/provenance", provenance_route))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.a2a_port, log_level="info")
