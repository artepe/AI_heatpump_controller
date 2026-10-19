import time
from unittest.mock import patch

import pytest

from custom_components.mango_tuya_local.client import MangoClient
from custom_components.mango_tuya_local.protocol import Profile

from .fake_device import Hardware
from .plant import OWNER_VALUES


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def hardware():
    hw = Hardware(Profile.load())
    hw.values.update(OWNER_VALUES)  # the owner's EEV tables and EEV step count
    hw.original = dict(hw.values)
    return hw


@pytest.fixture
def mocked_client(hardware):
    """Same simulated Tuya transport as the Mango Tuya Local test suite."""
    clients = []

    def create(host, device_id, local_key, profile):
        counter = iter(range(1 + len(clients) * 1000, 100000))
        client = MangoClient(
            host, device_id, local_key, profile,
            device_factory=hardware.factory, clock=lambda: 0, pause=lambda n: None,
            wall_clock=lambda: time.time(),  # follow the frozen test clock (the default binds the real one)
            header_factory=lambda size: next(counter).to_bytes(size, "big"),
        )
        clients.append(client)
        return client

    with (
        patch("custom_components.mango_tuya_local.MangoClient", side_effect=create),
        patch("custom_components.mango_tuya_local.config_flow.MangoClient", side_effect=create),
    ):
        yield clients
