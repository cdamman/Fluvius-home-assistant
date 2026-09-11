"""Fetch delayed Fluvius readings and publish their historical statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FluviusApiClient,
    FluviusApiError,
    FluviusAuthenticationError,
    FluviusDailySummary,
    FluviusPeakMeasurement,
    FluviusQuarterHourlyMeasurement,
)
from .const import DEFAULT_UPDATE_INTERVAL
from .statistics import FluviusStatistics
from .store import FluviusEnergyStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FluviusCoordinatorData:
    """Container returned by the coordinator."""

    latest_summary: FluviusDailySummary | None
    lifetime_totals: dict[str, float]
    peak_measurements: list[FluviusPeakMeasurement]
    quarter_hourly_measurements: list[FluviusQuarterHourlyMeasurement]


class FluviusEnergyDataUpdateCoordinator(DataUpdateCoordinator[FluviusCoordinatorData]):
    """Periodically fetch and store Fluvius energy data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FluviusApiClient,
        store: FluviusEnergyStore,
        statistics: FluviusStatistics | None = None,
    ) -> None:
        super().__init__(
            hass, LOGGER, name="Fluvius energy coordinator", update_interval=DEFAULT_UPDATE_INTERVAL
        )
        self._client = client
        self._store = store
        self._statistics = statistics

    async def _async_update_data(self) -> FluviusCoordinatorData:
        summaries, peaks, intervals = [], [], []
        errors = []
        try:
            summaries, peaks = await self._client.fetch_daily_summaries_with_spikes()
        except FluviusAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FluviusApiError as err:
            errors.append(err)

        if self._client.detailed_history:
            try:
                intervals = await self._client.fetch_quarter_hourly_consumption()
            except FluviusAuthenticationError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except FluviusApiError as err:
                errors.append(err)

        if errors and not summaries and not intervals:
            raise UpdateFailed(str(errors[0])) from errors[0]
        for err in errors:
            LOGGER.debug("Some Fluvius data is temporarily unavailable: %s", err)

        if self._statistics:
            await self._statistics.async_update(summaries, intervals)
        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        previous = self.data
        latest = summaries[-1] if summaries else (previous.latest_summary if previous else None)
        if latest is None and intervals:
            # Detailed-only meters still get a meaningful latest-period reading.
            item = intervals[-1]
            latest = FluviusDailySummary(item.start.isoformat(), item.start, item.end, item.metrics)
        if not summaries and not intervals:
            LOGGER.debug("No new published Fluvius readings; keeping the last available data")
        return FluviusCoordinatorData(
            latest_summary=latest,
            lifetime_totals=self._statistics.get_totals()
            if self._statistics
            else self._store.get_lifetime_totals(),
            peak_measurements=peaks or (previous.peak_measurements if previous else []),
            quarter_hourly_measurements=intervals
            or (previous.quarter_hourly_measurements if previous else []),
        )
