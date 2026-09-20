"""Exercise a real discovery provider only when explicitly configured."""

import os

import pytest


@pytest.mark.live
def test_real_discovery_requires_provider_configuration() -> None:
    """The live adapter/run is intentionally unavailable without an explicit provider credential."""
    if os.environ.get("CUA_RUN_LIVE_DISCOVERY") != "1":
        pytest.skip("Set CUA_RUN_LIVE_DISCOVERY=1 after configuring the target and provider")
    assert os.environ.get("ANTHROPIC_API_KEY"), (
        "Live discovery currently uses the Anthropic adapter"
    )
