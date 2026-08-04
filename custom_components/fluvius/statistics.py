"""Feed the sub-daily interval data into Home Assistant long term statistics.

A regular sensor cannot represent this data correctly: Fluvius publishes it a day
late and in sub-daily buckets, while a sensor state only ever describes *now*.
External statistics are the supported way to insert historical, timestamped energy
readings, and they are what the Energy dashboard consumes.
"""
from __future__ import annotations

from datetime import datetime
from functools import partial
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import FluviusIntervalMeasurement
from .const import (
    DOMAIN,
    METER_TYPE_GAS,
    METRIC_CONSUMPTION,
    METRIC_INJECTION,
    STATISTIC_ID_TEMPLATE,
    interval_key,
)

LOGGER = logging.getLogger(__name__)

# --- recorder metadata compatibility --------------------------------------
# The metadata schema keeps evolving and each core version rejects, or warns about,
# a different shape. Probe once at import instead of pinning a core version.

try:  # Home Assistant 2025.11+ replaced has_mean with mean_type
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_METADATA: Dict[str, Any] = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # pragma: no cover - older cores
    _MEAN_METADATA = {"has_mean": False}

# Recent cores also want the unit's conversion class. Older ones declare no such key
# and would carry it into the database untouched, so only send it where it exists.
_SUPPORTS_UNIT_CLASS = "unit_class" in getattr(StatisticMetaData, "__annotations__", {})

# Conversion class for each unit we can emit. Gas entries report either energy (kWh)
# or volume (m3) depending on the configured gas unit.
_UNIT_CLASSES = {
    UnitOfEnergy.KILO_WATT_HOUR: "energy",
    UnitOfVolume.CUBIC_METERS: "volume",
}

# Lower bound when reading a whole series back. Far enough in the past to cover any
# history Fluvius can serve; the query is indexed on statistic_id and start.
_SERIES_START = dt_util.utc_from_timestamp(0)


def build_statistic_id(ean: str, key: str) -> str:
    """Return the external statistic id for a given EAN and metric."""

    return STATISTIC_ID_TEMPLATE.format(domain=DOMAIN, ean=ean, key=key).lower()


async def async_stored_interval_hours(
    hass: HomeAssistant,
    ean: str,
    meter_type: str,
    since: datetime,
) -> Set[datetime]:
    """Return the UTC hours already imported for this meter, from `since` onwards.

    Lets the fetch skip days it would only re-download. The consumption series is the
    reference: it exists for every meter type, and injection is written alongside it.
    """

    statistic_id = build_statistic_id(ean, interval_key(meter_type, METRIC_CONSUMPTION))
    return set(await _async_stored_states(hass, statistic_id, since=since))


async def async_import_interval_statistics(
    hass: HomeAssistant,
    ean: str,
    meter_type: str,
    measurements: List[FluviusIntervalMeasurement],
    unit: str,
) -> None:
    """Import the interval measurements as external statistics."""

    if not measurements:
        return

    buckets = _bucket_hourly(measurements)
    if not buckets:
        return

    is_gas = meter_type == METER_TYPE_GAS
    resolution = "hourly" if is_gas else "quarter-hourly"

    series = [(METRIC_CONSUMPTION, f"Fluvius consumption ({resolution})", 0)]
    # Gas meters only ever consume, so an injection series would be a flat zero.
    if not is_gas:
        series.append((METRIC_INJECTION, f"Fluvius injection ({resolution})", 1))

    for metric, label, index in series:
        await _async_import_series(
            hass,
            build_statistic_id(ean, interval_key(meter_type, metric)),
            label,
            unit,
            buckets,
            index,
        )


def _bucket_hourly(
    measurements: List[FluviusIntervalMeasurement],
) -> Dict[datetime, Tuple[float, float]]:
    """Aggregate the measurements into the hourly buckets statistics require.

    Home Assistant stores external statistics on hour boundaries; the finer detail
    stays available in the sensor attributes.
    """

    accumulator: Dict[datetime, List[float]] = {}
    for item in measurements:
        hour = dt_util.as_utc(item.start).replace(minute=0, second=0, microsecond=0)
        values = accumulator.setdefault(hour, [0.0, 0.0])
        values[0] += item.consumption
        values[1] += item.injection
    return {hour: (values[0], values[1]) for hour, values in sorted(accumulator.items())}


async def _async_import_series(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    unit: str,
    buckets: Dict[datetime, Tuple[float, float]],
    index: int,
) -> None:
    """Insert the fetched hours into one metric's statistic series.

    Two paths, because the `sum` column is a running total and an hour inserted in
    the past invalidates every sum after it:

    * append -- the fetched hours are all newer than the series, so the sums simply
      continue. This is the every-refresh case.
    * rebuild -- older hours turned up (widening the lookback, or a day Fluvius
      published late). The whole series is then recomputed from zero so the sums stay
      consistent; `async_add_external_statistics` overwrites rows with the same start.
    """

    if not buckets:
        return

    running_sum, last_start = await _async_get_last_state(hass, statistic_id)
    earliest = min(buckets)

    if last_start is None:
        states = {hour: values[index] for hour, values in buckets.items()}
        stats = _rows_from_states(states, baseline=0.0)
        mode = "seeded"
    else:
        # Fluvius re-serves the same days on every refresh, so find what is actually
        # missing. Only the fetched window is read here; the whole series is loaded
        # further down, and only when a real backfill forces a rebuild.
        existing = await _async_stored_states(hass, statistic_id, since=earliest)
        missing = sorted(hour for hour in buckets if hour not in existing)

        if not missing:
            LOGGER.debug(
                "Statistics %s already up to date (last hour %s)", statistic_id, last_start
            )
            return

        if missing[0] > last_start:
            # Only newer hours: the running sum simply continues. Steady-state path.
            states = {hour: buckets[hour][index] for hour in missing}
            stats = _rows_from_states(states, baseline=running_sum)
            mode = "added"
        else:
            # Hours older than the newest stored one arrived, so every sum after them
            # is now wrong. Recompute the series from zero; async_add_external_statistics
            # overwrites rows sharing a start, so this repairs rather than duplicates.
            merged = await _async_stored_states(hass, statistic_id, since=_SERIES_START)
            merged.update({hour: values[index] for hour, values in buckets.items()})
            stats = _rows_from_states(merged, baseline=0.0)
            mode = "rebuilt"
            LOGGER.info(
                "Statistics %s: backfilling %d hour(s) older than %s; recomputing the "
                "running sum over %d hour(s) to keep it consistent.",
                statistic_id,
                len(missing),
                last_start,
                len(stats),
            )

    if not stats:
        return

    metadata: StatisticMetaData = {
        **_MEAN_METADATA,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if _SUPPORTS_UNIT_CLASS:
        metadata["unit_class"] = _UNIT_CLASSES.get(unit)
    async_add_external_statistics(hass, metadata, stats)
    running_sum = float(stats[-1]["sum"] or 0.0)
    LOGGER.debug(
        "Statistics %s: %s %d hour(s), %s -> %s, running sum %.4f %s",
        statistic_id,
        mode,
        len(stats),
        stats[0]["start"].isoformat(),
        stats[-1]["start"].isoformat(),
        running_sum,
        unit,
    )


def _rows_from_states(
    states: Dict[datetime, float],
    baseline: float,
) -> List[StatisticData]:
    """Turn hour -> value into statistic rows carrying a running sum."""

    running = baseline
    rows: List[StatisticData] = []
    for hour in sorted(states):
        value = states[hour]
        running += value
        rows.append(StatisticData(start=hour, state=round(value, 4), sum=round(running, 4)))
    return rows


async def _async_stored_states(
    hass: HomeAssistant,
    statistic_id: str,
    since: datetime,
) -> Dict[datetime, float]:
    """Return the stored hours from `since` onwards, as hour -> per-hour value.

    Reads `state` rather than `sum`: the per-hour values are what a rebuild needs, and
    they let the sums be recomputed from zero without trusting the old ones.
    """

    rows = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            since,
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
    )

    states: Dict[datetime, float] = {}
    for row in (rows or {}).get(statistic_id, []):
        start = _as_datetime(row.get("start"))
        if start is None:
            continue
        value = row.get("state")
        states[start] = float(value) if value is not None else 0.0
    return states


async def _async_get_last_state(
    hass: HomeAssistant,
    statistic_id: str,
) -> Tuple[float, Optional[datetime]]:
    """Return the running sum and timestamp of the newest stored statistic."""

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum", "start"},
    )
    rows = (last_stats or {}).get(statistic_id)
    if not rows:
        return 0.0, None

    row = rows[0]
    running_sum = float(row.get("sum") or 0.0)
    return running_sum, _as_datetime(row.get("start"))


def _as_datetime(value: object) -> Optional[datetime]:
    """Normalise the `start` field, stored as a timestamp on recent cores."""

    if isinstance(value, datetime):
        return dt_util.as_utc(value)
    if isinstance(value, (int, float)):
        return dt_util.utc_from_timestamp(float(value))
    return None
