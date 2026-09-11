"""Enable local integration loading in Home Assistant tests."""

import pytest


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Configure the isolated SQLite database before Home Assistant starts."""


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load the integration under test."""
