"""Tier escalation and degraded-branch signals (A5)."""

import sys

sys.path.insert(0, "agents")
sys.path.insert(0, "services/api")
from run_metrics import metric_payload  # noqa: E402
from shared.models import model_unavailable  # noqa: E402


class Client(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class ModelThrottledException(Exception):
    pass


def test_model_unavailable_recognises_provider_side_failures():
    assert model_unavailable(Client("ThrottlingException"))
    assert model_unavailable(Client("ServiceUnavailableException"))
    assert model_unavailable(Client("ModelNotReadyException"))
    assert model_unavailable(ModelThrottledException("rate exceeded"))
    assert model_unavailable(RuntimeError("Rate exceeded: throttled by the model"))
    assert not model_unavailable(Client("ValidationException"))
    assert not model_unavailable(ValueError("no JSON object in the answer"))


def test_degraded_branches_and_escalations_are_counted():
    manifest = {
        "nodes": [
            {"node": "investigator_identity", "degraded": True},
            {"node": "investigator_movement"},
            {"node": "report", "escalated_to": "strong"},
            {"node": "report", "tier": "strong"},
        ]
    }
    by_name = {m["MetricName"]: m["Value"] for m in metric_payload(manifest)}
    assert by_name == {"Completed": 1.0, "DegradedBranches": 1.0, "Escalations": 1.0}
    assert {m["MetricName"] for m in metric_payload({})} == {
        "Completed",
        "DegradedBranches",
        "Escalations",
    }
