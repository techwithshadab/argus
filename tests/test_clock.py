"""The clock rule: the scenario clock in replay, the wall clock in live mode."""

import sys
from datetime import UTC, datetime

sys.path.insert(0, "agents")
from shared.config import scenario_clock  # noqa: E402


def test_replay_uses_the_scenario_end():
    assert scenario_clock("2026-09-01T08:00:00Z", "replay") == "2026-09-01T08:00:00Z"


def test_live_uses_the_wall_clock():
    now = datetime.fromisoformat(
        scenario_clock("2026-09-01T08:00:00Z", "live").replace("Z", "+00:00")
    )
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5
