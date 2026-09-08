"""Inspect AI task: one sample per injected anomaly; the scorer checks the live API for a matching alert.
Run:  inspect eval evals/inspect_task.py --model bedrock/us.anthropic.claude-sonnet-4-6   (model is unused by the scorer)"""

from __future__ import annotations

import json
import os

import httpx
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState, solver
from run_eval import overlap

API = os.getenv("API_URL", "http://localhost:8000")


@solver
def passthrough():
    async def solve(state: TaskState, generate):
        return state

    return solve


@scorer(metrics=[accuracy()])
def alert_exists():
    async def score(state: TaskState, target: Target) -> Score:
        t = json.loads(state.input_text)
        alerts = httpx.get(f"{API}/alerts").json()
        ok = any(
            a["mmsi"] == t["mmsi"]
            and a["kind"] == t["kind"]
            and a.get("started_at")
            and a.get("ended_at")
            and overlap(t["started_at"], t["ended_at"], a["started_at"], a["ended_at"])
            for a in alerts
        )
        return Score(
            value=CORRECT if ok else INCORRECT,
            explanation=f"{t['kind']} for {t['mmsi']}",
        )

    return score


@task
def watch_agent_recall():
    truth = httpx.get(f"{API}/ground-truth").json()
    return Task(
        dataset=[Sample(input=json.dumps(t), target="alert") for t in truth],
        solver=passthrough(),
        scorer=alert_exists(),
    )
