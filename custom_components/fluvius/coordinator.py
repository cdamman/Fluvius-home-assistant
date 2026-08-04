"""DataUpdateCoordinator for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Dict, List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FluviusApiClient,
    FluviusApiError,
    FluviusDailySummary,
    FluviusPeakMeasurement,
    FluviusIntervalMeasurement,
)
from .const import DEFAULT_UPDATE_INTERVAL
from .statistics import (
    async_import_interval_statistics,
    async_stored_interval_hours,
)
from .store import FluviusEnergyStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FluviusCoordinatorData:
    """Container returned by the coordinator."""

    latest_summary: FluviusDailySummary | None
    lifetime_totals: Dict[str, float]
    peak_measurements: list[FluviusPeakMeasurement]
    interval_measurements: List[FluviusIntervalMeasurement]


class FluviusEnergyDataUpdateCoordinator(DataUpdateCoordinator[FluviusCoordinatorData]):
    """Periodically fetch and store Fluvius energy data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FluviusApiClient,
        store: FluviusEnergyStore,
        ean: str,
        meter_type: str,
        statistics_unit: str,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name="Fluvius energy coordinator",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self._client = client
        self._store = store
        self._ean = ean
        self._meter_type = meter_type
        self._statistics_unit = statistics_unit
        # Interval data is historical and immutable once published, so keeping it
        # between refreshes lets the fetch ask only for days it does not have yet
        # while the sensors keep showing the last published day.
        self._interval_cache: Dict[datetime, FluviusIntervalMeasurement] = {}

    async def _async_already_imported_hours(self) -> set[datetime] | None:
        """Hours the fetch can skip because the statistics already hold them.

        Returns None on the first refresh of a Home Assistant run: the cache is empty
        then, so the whole window has to be read back to give the sensors a value even
        though the statistics are complete.
        """

        if not self._interval_cache:
            return None
        try:
            return await async_stored_interval_hours(
                self.hass,
                self._ean,
                self._meter_type,
                since=self._client.interval_window_start(),
            )
        except Exception as err:  # noqa: BLE001 - fall back to fetching everything
            LOGGER.debug(
                "Could not read imported hours (%s: %s); fetching the whole window",
                type(err).__name__,
                err,
            )
            return None

    def _merge_interval_cache(
        self,
        fetched: List[FluviusIntervalMeasurement],
    ) -> List[FluviusIntervalMeasurement]:
        """Fold newly fetched intervals into the cache and drop anything too old."""

        for item in fetched:
            self._interval_cache[item.start] = item

        window_start = self._client.interval_window_start()
        self._interval_cache = {
            start: item
            for start, item in self._interval_cache.items()
            if start >= window_start
        }
        return [self._interval_cache[start] for start in sorted(self._interval_cache)]

    async def _async_update_data(self) -> FluviusCoordinatorData:
        start_time = time.monotonic()
        LOGGER.debug("=== FLUVIUS UPDATE START ===")
        
        # Step 1: Fetch daily summaries and peak power
        try:
            LOGGER.debug("Step 1/3: Fetching daily consumption summaries and peak power...")
            summaries, peak_measurements = await self._client.fetch_daily_summaries_with_spikes()
            LOGGER.debug(
                "Step 1/3: SUCCESS - Received %d daily summaries, %d peak measurements",
                len(summaries),
                len(peak_measurements),
            )
        except FluviusApiError as err:
            elapsed = time.monotonic() - start_time
            LOGGER.error(
                "=== FLUVIUS UPDATE FAILED (%.2fs) === Step 1/3 failed: %s. "
                "Check the errors above for more details. Common causes: "
                "1) Authentication expired - try reloading the integration, "
                "2) Fluvius service is temporarily unavailable, "
                "3) Invalid EAN or meter serial number.",
                elapsed,
                err,
            )
            raise UpdateFailed(str(err)) from err

        if not summaries:
            LOGGER.warning(
                "Step 1/3: WARNING - No daily consumption data returned. "
                "This can happen if: 1) Your meter is newly installed, "
                "2) Fluvius hasn't processed recent data yet, "
                "3) The configured date range has no data."
            )

        # Step 2: Fetch sub-daily interval data (non-blocking on failure)
        resolution = self._client.interval_minutes
        fetched: list[FluviusIntervalMeasurement] = []
        try:
            LOGGER.debug("Step 2/3: Fetching %d-minute consumption data...", resolution)
            fetched = await self._client.fetch_interval_consumption(
                skip_hours=await self._async_already_imported_hours()
            )
            LOGGER.debug("Step 2/3: SUCCESS - Received %d intervals", len(fetched))
            if fetched:
                LOGGER.debug(
                    "Step 2/3: Covered range %s -> %s, consumption total %.3f, injection total %.3f",
                    fetched[0].start.isoformat(),
                    fetched[-1].end.isoformat(),
                    sum(item.consumption for item in fetched),
                    sum(item.injection for item in fetched),
                )
            elif self._interval_cache:
                LOGGER.debug(
                    "Step 2/3: Nothing new to fetch; keeping %d cached interval(s)",
                    len(self._interval_cache),
                )
            elif self._client.interval_unavailable:
                # Already reported once by the client, with the full probe report.
                LOGGER.debug(
                    "Step 2/3: No sub-daily data for this meter; interval fetch is disabled."
                )
            else:
                LOGGER.warning(
                    "Step 2/3: The interval API replied but contained no usable %d-minute "
                    "measurement. Those sensors will stay unknown. Enable verbose logging in "
                    "the integration options to dump the raw payload.",
                    resolution,
                )
        # Any failure here must stay non-fatal: aiohttp raises asyncio.TimeoutError
        # (neither a ClientError nor a FluviusApiError) when the 30s timeout expires,
        # which would otherwise take down every sensor of the entry.
        except Exception as err:  # noqa: BLE001 - deliberately broad, see above
            LOGGER.warning(
                "Step 2/3: SKIPPED - Could not fetch interval data: %s: %s. "
                "This is non-fatal; daily data will still work.",
                type(err).__name__,
                err,
            )

        intervals = self._merge_interval_cache(fetched)

        # Step 2b: Push the interval data into long term statistics. This is what the
        # Energy dashboard reads; the sensors below are informational only.
        if intervals:
            try:
                await async_import_interval_statistics(
                    self.hass,
                    self._ean,
                    self._meter_type,
                    intervals,
                    self._statistics_unit,
                )
            except Exception as err:  # noqa: BLE001 - never break the update over statistics
                LOGGER.warning(
                    "Could not import interval statistics: %s: %s",
                    type(err).__name__,
                    err,
                )

        # Step 3: Process and store data
        LOGGER.debug("Step 3/3: Processing and storing %d summaries...", len(summaries))
        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        totals = self._store.get_lifetime_totals()
        latest_summary = summaries[-1] if summaries else None
        
        elapsed = time.monotonic() - start_time
        LOGGER.debug(
            "=== FLUVIUS UPDATE COMPLETE (%.2fs) === "
            "Daily summaries: %d, Peak measurements: %d, Interval measurements: %d",
            elapsed,
            len(summaries),
            len(peak_measurements),
            len(intervals),
        )
        
        return FluviusCoordinatorData(
            latest_summary=latest_summary,
            lifetime_totals=totals,
            peak_measurements=peak_measurements,
            interval_measurements=intervals,
        )
