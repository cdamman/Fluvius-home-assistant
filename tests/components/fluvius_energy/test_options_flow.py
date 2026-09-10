"""Tests for the Fluvius Energy options flow."""

from __future__ import annotations

import pytest
from homeassistant import data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fluvius.const import (
    CONF_EAN,
    CONF_GRANULARITY,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
)

pytestmark = pytest.mark.usefixtures("recorder_mock")

USER_INPUT = {
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "password",
    CONF_EAN: "541448800000000000",
    CONF_METER_SERIAL: "1SAGTEST",
    CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
}


async def test_options_flow(hass):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_GRANULARITY: "1",
            CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GRANULARITY] == "1"


async def test_gas_hourly_option_and_clear_cutoff(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_METER_TYPE: "gas"},
        options={"history_until": "2026-04-01T00:00:00+02:00"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_GRANULARITY: "2", CONF_METER_TYPE: "gas"}
    )
    assert result["data"][CONF_GRANULARITY] == "2"
    assert "history_until" not in result["data"]


async def test_invalid_timezone_is_form_error(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"timezone": "Invalid/Zone", CONF_GRANULARITY: "1"}
    )
    assert result["errors"]["base"] == "invalid_history"
