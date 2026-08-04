"""Sensor platform for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import FluviusIntervalMeasurement, FluviusPeakMeasurement
from .const import (
    CONF_EAN,
    CONF_GAS_UNIT,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DEFAULT_GAS_UNIT,
    DEFAULT_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
    METRIC_CONSUMPTION,
    METRIC_INJECTION,
    GAS_UNIT_CUBIC_METERS,
    interval_key,
)
from .coordinator import FluviusCoordinatorData, FluviusEnergyDataUpdateCoordinator
from .models import FluviusRuntimeData
from .statistics import build_statistic_id


@dataclass(frozen=True, slots=True, kw_only=True)
class FluviusEnergySensorEntityDescription(SensorEntityDescription):
    """Describe a Fluvius Energy sensor."""

    metric: str
    is_lifetime: bool = True


SENSOR_TYPES: tuple[FluviusEnergySensorEntityDescription, ...] = (
    FluviusEnergySensorEntityDescription(
        key="consumption_total",
        translation_key="consumption_total",
        name="Fluvius consumption",
        metric="consumption_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_high",
        translation_key="consumption_high",
        name="Fluvius consumption (high tariff)",
        metric="consumption_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_low",
        translation_key="consumption_low",
        name="Fluvius consumption (low tariff)",
        metric="consumption_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_total",
        translation_key="injection_total",
        name="Fluvius injection",
        metric="injection_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_high",
        translation_key="injection_high",
        name="Fluvius injection (high tariff)",
        metric="injection_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_low",
        translation_key="injection_low",
        name="Fluvius injection (low tariff)",
        metric="injection_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="net_consumption_day",
        translation_key="net_consumption_day",
        name="Fluvius net consumption (day)",
        metric="net_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        is_lifetime=False,
    ),
)

PEAK_POWER_DESCRIPTION = SensorEntityDescription(
    key="peak_power",
    translation_key="peak_power",
    name="Fluvius peak power",
    device_class=SensorDeviceClass.POWER,
    native_unit_of_measurement=UnitOfPower.KILO_WATT,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=3,
)

def _interval_descriptions(
    meter_type: str,
    use_gas_volume: bool,
) -> list[tuple[SensorEntityDescription, str]]:
    """Build the sub-daily sensor descriptions for this meter.

    Electricity is registered per quarter-hour, gas per hour, so the entity keys and
    labels follow the meter's own resolution rather than a hard-coded "quarter".

    These sensors report the total of the most recent day Fluvius has published, with
    the per-interval breakdown in the attributes. They deliberately carry NO
    state_class: the value is a historical daily total that jumps to an unrelated
    figure every day, which any statistics state class would misread as a meter reset.
    The Energy dashboard must use the external statistics (see statistics.py) instead.
    """

    is_gas = meter_type == METER_TYPE_GAS
    resolution = "hourly" if is_gas else "quarter-hourly"
    shared = {
        "device_class": SensorDeviceClass.GAS if use_gas_volume else SensorDeviceClass.ENERGY,
        "native_unit_of_measurement": (
            UnitOfVolume.CUBIC_METERS if use_gas_volume else UnitOfEnergy.KILO_WATT_HOUR
        ),
        "suggested_display_precision": 3,
    }

    # A gas meter only ever consumes; an injection sensor would sit at zero forever.
    metrics = (METRIC_CONSUMPTION,) if is_gas else (METRIC_CONSUMPTION, METRIC_INJECTION)
    return [
        (
            SensorEntityDescription(
                key=interval_key(meter_type, metric),
                translation_key=interval_key(meter_type, metric),
                name=f"Fluvius {metric} ({resolution})",
                **shared,
            ),
            metric,
        )
        for metric in metrics
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Fluvius sensors."""

    runtime_data: FluviusRuntimeData = entry.runtime_data
    coordinator: FluviusEnergyDataUpdateCoordinator = runtime_data.coordinator
    ean = entry.data[CONF_EAN]
    meter_serial = entry.data[CONF_METER_SERIAL]
    meter_type = entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE)
    gas_unit = entry.options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT)
    use_gas_volume = meter_type == METER_TYPE_GAS and gas_unit == GAS_UNIT_CUBIC_METERS

    descriptions = SENSOR_TYPES
    if use_gas_volume:
        descriptions = [
            replace(
                description,
                device_class=SensorDeviceClass.GAS,
                native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            )
            for description in SENSOR_TYPES
        ]

    entities = [
        FluviusEnergySensor(
            description, coordinator, entry.entry_id, ean, meter_serial, meter_type
        )
        for description in descriptions
    ]
    if meter_type == METER_TYPE_ELECTRICITY:
        entities.append(
            FluviusPeakPowerSensor(PEAK_POWER_DESCRIPTION, coordinator, entry.entry_id, ean, meter_serial)
        )
    
    entities.extend(
        FluviusIntervalSensor(
            description, coordinator, entry.entry_id, ean, meter_serial, metric
        )
        for description, metric in _interval_descriptions(meter_type, use_gas_volume)
    )
    async_add_entities(entities)


class FluviusEnergySensor(CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity):
    """Define a Fluvius energy sensor."""

    entity_description: FluviusEnergySensorEntityDescription

    def __init__(
        self,
        description: FluviusEnergySensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
        meter_type: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )
        # A gas meter cannot inject. Reporting a flat 0 would read as a real
        # measurement, so the entity is kept (dashboards referencing it survive)
        # but marked unavailable.
        self._not_applicable = (
            meter_type == METER_TYPE_GAS
            and description.metric.startswith(METRIC_INJECTION)
        )

    @property
    def available(self) -> bool:
        if self._not_applicable:
            return False
        return super().available

    @property
    def native_value(self) -> Optional[float]:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if data is None or self._not_applicable:
            return None
        metric = self.entity_description.metric
        if self.entity_description.is_lifetime:
            value = data.lifetime_totals.get(metric)
        else:
            latest = data.latest_summary
            value = latest.metrics.get(metric) if latest else None
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if data is None or data.latest_summary is None or self._not_applicable:
            return None
        latest = data.latest_summary
        attributes: Dict[str, Any] = {
            "latest_period_start": latest.start.isoformat(),
            "latest_period_end": latest.end.isoformat(),
            "latest_consumption": round(latest.metrics.get("consumption_total", 0.0), 3),
            "latest_injection": round(latest.metrics.get("injection_total", 0.0), 3),
        }
        if not self.entity_description.is_lifetime:
            attributes["latest_net_consumption"] = round(latest.metrics.get("net_consumption", 0.0), 3)
        return attributes


class FluviusPeakPowerSensor(CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity):
    """Expose the monthly peak power reported by Fluvius."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _latest_peak(self) -> FluviusPeakMeasurement | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.peak_measurements:
            return None
        return data.peak_measurements[-1]

    @property
    def native_value(self) -> Optional[float]:
        latest = self._latest_peak()
        if not latest:
            return None
        return round(latest.value_kw, 3)

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        data: FluviusCoordinatorData | None = self.coordinator.data
        latest = self._latest_peak()
        if not data or not latest:
            return None
        history = {
            peak.period_start.strftime("%Y-%m"): round(peak.value_kw, 3)
            for peak in data.peak_measurements[-12:]
        }
        return {
            "period_start": latest.period_start.isoformat(),
            "period_end": latest.period_end.isoformat(),
            "spike_window_start": latest.spike_start.isoformat(),
            "spike_window_end": latest.spike_end.isoformat(),
            "monthly_peaks_kw": history,
        }


class FluviusIntervalSensor(
    CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity
):
    """Report the most recent day published by Fluvius at the meter's resolution.

    The state is the total of that day, not a running counter: Fluvius serves this
    data one day late, so a cumulative state class would be misinterpreted by the
    statistics engine. The exact history is inserted as external statistics instead.
    """

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
        metric: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._ean = ean
        # METRIC_* match the FluviusIntervalMeasurement field names.
        self._metric = metric
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _value_of(self, measurement: FluviusIntervalMeasurement) -> float:
        """Return the field this sensor tracks."""
        return float(getattr(measurement, self._metric))

    def _last_day_intervals(self) -> list[FluviusIntervalMeasurement]:
        """Return the intervals belonging to the most recent covered day.

        Anchored on the last interval rather than on the calendar date: the gas day
        runs 06:00 -> 06:00, so grouping by date would split it across two days.
        """
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.interval_measurements:
            return []
        measurements = data.interval_measurements
        window_start = measurements[-1].end - timedelta(days=1)
        return [item for item in measurements if item.start >= window_start]

    @property
    def native_value(self) -> Optional[float]:
        intervals = self._last_day_intervals()
        if not intervals:
            return None
        return round(sum(self._value_of(item) for item in intervals), 3)

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        data: FluviusCoordinatorData | None = self.coordinator.data
        intervals = self._last_day_intervals()
        if not data or not intervals:
            return None

        measurements = data.interval_measurements
        return {
            # Start of the covered day, which for gas is 06:00 rather than midnight.
            "day_start": intervals[0].start.isoformat(),
            "period_start": intervals[0].start.isoformat(),
            "period_end": intervals[-1].end.isoformat(),
            "data_from": measurements[0].start.isoformat(),
            "data_until": measurements[-1].end.isoformat(),
            "interval_count": len(intervals),
            "total_interval_count": len(measurements),
            # Point users at the series the Energy dashboard should consume.
            "statistic_id": build_statistic_id(self._ean, self.entity_description.key),
            self.entity_description.key: {
                item.start.isoformat(): round(self._value_of(item), 3)
                for item in intervals
            },
        }
