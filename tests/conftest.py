"""Test fixtures."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/lutron_vive."""
    return


@pytest.fixture
async def mock_hub(socket_enabled):
    """A simulated Vive hub on localhost."""
    from mock_hub import MockHub

    hub = MockHub()
    await hub.start()
    yield hub
    await hub.stop()
