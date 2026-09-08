"""Hand-edited .env files: inline comments must never become model ids or keys."""

import sys

sys.path.insert(0, "agents")
from shared.config import clean_env_value  # noqa: E402


def test_inline_comment_is_dropped():
    assert (
        clean_env_value("us.amazon.nova-pro-v1:0   # strong tier")
        == "us.amazon.nova-pro-v1:0"
    )


def test_comment_only_value_counts_as_unset():
    assert clean_env_value("# optional Bedrock Guardrail", "") == ""
    assert clean_env_value("  # note", "x") == "x"


def test_plain_values_pass_through():
    assert clean_env_value("live") == "live"
    assert clean_env_value(None, "replay") == "replay"
