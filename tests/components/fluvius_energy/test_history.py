"""Regression tests for delayed, corrected and overlapping consumption history."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.fluvius.api import (
    FluviusApiClient,
    FluviusDailySummary,
    FluviusQuarterHourlyMeasurement,
)
from custom_components.fluvius.const import CONF_DAYS_BACK, CONF_GRANULARITY, CONF_HISTORY_UNTIL
from custom_components.fluvius.statistics import FluviusStatistics, statistic_prefix
from custom_components.fluvius.store import FluviusEnergyStore

UTC = UTC


def client(**options):
    return FluviusApiClient(
        session=MagicMock(),
        email="test@example.com",
        password="test",
        ean="541448800000000000",
        meter_serial="TEST",
        options=options,
    )


def day(start, value, hours=24):
    return FluviusDailySummary(
        start.isoformat(), start, start + timedelta(hours=hours), {"consumption_high": value}
    )


def intervals(start, count, value=1):
    return [
        FluviusQuarterHourlyMeasurement(
            start + timedelta(minutes=i * 15),
            start + timedelta(minutes=(i + 1) * 15),
            value,
            0,
            {"consumption_high": value},
        )
        for i in range(count)
    ]


def history(hass):
    obj = FluviusStatistics(hass, "test_electricity_kwh", "Test", "kWh")
    obj._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    return obj


@pytest.mark.parametrize(
    ("local_date", "count"), [("2026-03-29", 92), ("2026-10-25", 100), ("2026-04-01", 96)]
)
async def test_dst_and_actual_usage_date(hass, local_date, count):
    local = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo("Europe/Brussels"))
    start = local.astimezone(UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([day(start, count, count // 4)], intervals(start, count))
    hourly = obj._hours()
    assert len(hourly) == count // 4
    assert sum(row["consumption_total"] for row in hourly.values()) == count
    assert {t.astimezone(local.tzinfo).date() for t in hourly} == {local.date()}
    rows = add.call_args_list[0].args[2]
    assert rows[0]["sum"] == 0
    assert rows[-1]["sum"] == count
    assert all(t["start"].minute == 0 for t in rows)


async def test_corrections_reimports_and_restart(hass):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([day(start, 10), day(start + timedelta(days=1), 20)], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 30
        add.reset_mock()
        await obj.async_update([day(start, 10)], [])
        add.assert_not_called()
        await obj.async_update([day(start, 7)], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 27
        recreated = history(hass)
        recreated._store.async_load.return_value = obj._data
        await recreated.async_load()
        add.reset_mock()
        await recreated.async_update([], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 27


async def test_detail_replaces_daily_without_double_counting(hass):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        await obj.async_update([day(start, 96)], [])
        await obj.async_update([], intervals(start, 95))
        assert obj._hours()[start]["consumption_total"] == 96  # incomplete: retain daily total
        await obj.async_update([], intervals(start, 96))
        assert obj._hours()[start]["consumption_total"] == 4
        assert sum(v["consumption_total"] for v in obj._hours().values()) == 96
        await obj.async_update([], intervals(start, 1, value=0.5))
        assert sum(v["consumption_total"] for v in obj._hours().values()) == 95.5


async def test_store_downward_correction_and_long_lookback(hass):
    store = FluviusEnergyStore(hass, "test", "kwh")
    store._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    for i in range(90):
        await store.async_process_summary(
            (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
            {"consumption_high": 1},
        )
    first = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    await store.async_process_summary(first, {"consumption_high": 1})
    assert store.get_lifetime_totals()["consumption_total"] == 90
    await store.async_process_summary(first, {"consumption_high": 0.5})
    assert store.get_lifetime_totals()["consumption_total"] == 89.5
    assert store.get_last_day_id() != first


async def test_fetches_entire_window_and_empty_recent_days():
    obj = client(**{CONF_DAYS_BACK: 7, CONF_GRANULARITY: "3"})
    obj._async_get_access_token = AsyncMock(return_value="test-token")
    obj._fetch_raw_quarter_hourly = AsyncMock(return_value=[])
    await obj.fetch_quarter_hourly_consumption()
    assert [call.args[1] for call in obj._fetch_raw_quarter_hourly.call_args_list] == list(
        range(7, 0, -1)
    )
    obj._async_get_access_token.assert_awaited_once()


def test_cutoff_excludes_overlap_and_has_separate_statistics():
    cutoff = "2026-04-01T12:30:00+02:00"
    obj = client(**{CONF_HISTORY_UNTIL: cutoff})
    assert obj.history_end == datetime.fromisoformat(cutoff)
    assert (
        obj._summaries_from_payload(
            [
                {
                    "d": "2026-03-31T22:00:00Z",
                    "de": "2026-04-01T22:00:00Z",
                    "v": [{"dc": 1, "t": 1, "u": 3, "v": 10}],
                }
            ]
        )
        == []
    )
    assert statistic_prefix("ean", "electricity", "kwh") != statistic_prefix(
        "ean", "electricity", "kwh", cutoff
    )
    assert statistic_prefix("ean", "gas", "kwh") != statistic_prefix("ean", "gas", "m3")


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", -1, "invalid"])
def test_missing_invalid_readings_do_not_become_zero(value):
    obj = client()
    assert (
        obj._summarize_day(
            {"d": "2026-04-01T00:00:00Z", "v": [{"dc": 1, "t": 1, "u": 3, "v": value}]}
        )
        is None
    )


async def test_real_recorder_import_is_idempotent(hass, recorder_mock):
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_recorder_block_till_done,
    )

    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    await obj.async_update([], intervals(start, 8))
    await async_recorder_block_till_done(hass)
    statistic_id = "fluvius:test_electricity_kwh_consumption_total"
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(hours=3),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    assert [v["sum"] for v in rows[statistic_id]] == [0, 4, 8]
    await obj.async_update([], intervals(start, 1, 0.5))
    await async_recorder_block_till_done(hass)
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(hours=3),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    assert [v["sum"] for v in rows[statistic_id]] == [0, 3.5, 7.5]


async def test_cutoff_filters_intervals_and_deduplicates():
    obj = client(**{CONF_HISTORY_UNTIL: "2026-04-01T12:30:00+02:00"})
    obj._async_get_access_token = AsyncMock(return_value="token")
    payload = [
        {
            "d": "2026-04-01T10:15:00Z",
            "de": "2026-04-01T10:30:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 1}],
        },
        {
            "d": "2026-04-01T10:30:00Z",
            "de": "2026-04-01T10:45:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 2}],
        },
    ]
    obj._fetch_raw_quarter_hourly = AsyncMock(return_value=payload)
    result = await obj.fetch_quarter_hourly_consumption(days_back=2)
    assert len(result) == 1
    assert result[0].consumption == 1
    assert result[0].end == obj.history_end


@pytest.mark.parametrize(("meter_type", "granularity"), [("electricity", "1"), ("gas", "2")])
async def test_interval_request_granularity(meter_type, granularity):
    obj = client()
    obj._meter_type = meter_type
    obj._request_history = AsyncMock(return_value=[])
    await obj._fetch_raw_quarter_hourly("token", 1)
    assert obj._request_history.call_args.args[1]["granularity"] == granularity


async def test_access_token_is_reused():
    obj = client()
    with patch(
        "custom_components.fluvius.api.async_get_bearer_token",
        AsyncMock(return_value=("token", {"expires_in": 3600})),
    ) as auth:
        assert await obj._async_get_access_token() == "token"
        assert await obj._async_get_access_token() == "token"
        auth.assert_awaited_once()


async def test_timeout_is_retryable():
    from custom_components.fluvius.api import FluviusApiError

    obj = client()
    obj._session.get.side_effect = TimeoutError
    with pytest.raises(FluviusApiError):
        await obj._fetch_raw_consumption("token")


async def test_empty_refresh_preserves_data_and_auth_triggers_reauth(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.fluvius.api import FluviusAuthenticationError
    from custom_components.fluvius.coordinator import FluviusEnergyDataUpdateCoordinator

    obj = client(**{CONF_GRANULARITY: "1"})
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj.fetch_daily_summaries_with_spikes = AsyncMock(return_value=([], []))
    obj.fetch_quarter_hourly_consumption = AsyncMock(return_value=intervals(start, 4))
    store = MagicMock(get_lifetime_totals=lambda: {})
    stats = history(hass)
    coordinator = FluviusEnergyDataUpdateCoordinator(hass, obj, store, stats)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data.lifetime_totals["consumption_total"] == 4
        obj.fetch_quarter_hourly_consumption.return_value = []
        second = await coordinator._async_update_data()
        assert second.quarter_hourly_measurements == coordinator.data.quarter_hourly_measurements
        assert second.latest_summary is not None
        obj.fetch_daily_summaries_with_spikes.side_effect = FluviusAuthenticationError(
            "credentials"
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()


async def test_real_recorder_gas_volume(hass, recorder_mock):
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_recorder_block_till_done,
    )

    obj = FluviusStatistics(hass, "test_gas_m3", "Gas", "m3")
    obj._store = MagicMock(async_save=AsyncMock())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    await obj.async_update([day(start, 3)], [])
    await async_recorder_block_till_done(hass)
    key = "fluvius:test_gas_m3_consumption_total"
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(days=1),
        {key},
        "hour",
        None,
        {"sum"},
    )
    assert rows[key][-1]["sum"] == 3


async def test_older_noncontiguous_backfill_overwrites_previous_baseline(hass):
    start = datetime(2026, 4, 3, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([], intervals(start, 4))
        add.reset_mock()
        await obj.async_update([], intervals(start - timedelta(days=2), 4))
    rows = {r["start"]: r["sum"] for r in add.call_args_list[0].args[2]}
    assert rows[start - timedelta(hours=1)] == 4
    assert rows[start] == 8


def test_cutoff_lookback_uses_local_dst_offsets():
    obj = client(**{CONF_HISTORY_UNTIL: "2026-03-30T00:00:00+02:00"})
    date_range = obj._build_quarter_hourly_range(2)
    assert date_range["historyFrom"] == "2026-03-28T00:00:00.000+01:00"
    assert date_range["historyUntil"] == "2026-03-28T23:59:59.999+01:00"
    dst_day = obj._build_quarter_hourly_range(1)
    start = datetime.fromisoformat(dst_day["historyFrom"])
    end = datetime.fromisoformat(dst_day["historyUntil"]) + timedelta(milliseconds=1)
    assert end - start == timedelta(hours=23)
