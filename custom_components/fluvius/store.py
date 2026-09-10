"""Persistent storage for Fluvius lifetime energy statistics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import GAS_UNIT_KWH, LIFETIME_METRICS, STORAGE_KEY_TEMPLATE, STORAGE_VERSION


class FluviusEnergyStore:
    """Wrap Home Assistant Store helper to accumulate total energy values."""

    def __init__(self, hass: HomeAssistant, entry_id: str, unit: str) -> None:
        key = STORAGE_KEY_TEMPLATE.format(entry_id=entry_id)
        self._store = Store(hass, STORAGE_VERSION, key)
        self._unit = unit
        self._data: dict[str, dict] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        stored_unit = (data or {}).get("unit", GAS_UNIT_KWH)
        if not data or stored_unit != self._unit:
            data = {"days": {}, "totals": {}, "last_day": None, "unit": self._unit}
        for metric in LIFETIME_METRICS:
            data["totals"].setdefault(metric, 0.0)
        data.setdefault("unit", self._unit)
        self._data = data

    async def async_process_summary(self, summary_day_id: str, metrics: dict[str, float]) -> None:
        if self._data is None:
            await self.async_load()
        assert self._data is not None

        day_store = self._data["days"].setdefault(summary_day_id, {})
        changed = False
        for metric in LIFETIME_METRICS:
            new_value = round(metrics.get(metric, 0.0), 4)
            prev_value = round(day_store.get(metric, 0.0), 4)
            delta = round(new_value - prev_value, 4)
            if delta != 0:
                day_store[metric] = new_value
                self._data["totals"][metric] = round(
                    self._data["totals"].get(metric, 0.0) + delta, 4
                )
                changed = True
        if changed:
            self._data["days"][summary_day_id] = day_store
            self._data["last_day"] = max(
                summary_day_id, self._data.get("last_day") or summary_day_id
            )
            await self._store.async_save(self._data)

    def get_lifetime_totals(self) -> dict[str, float]:
        if self._data is None:
            base = {metric: 0.0 for metric in LIFETIME_METRICS}
        else:
            base = {
                metric: round(self._data["totals"].get(metric, 0.0), 4)
                for metric in LIFETIME_METRICS
            }
        base["consumption_total"] = round(base["consumption_high"] + base["consumption_low"], 4)
        base["injection_total"] = round(base["injection_high"] + base["injection_low"], 4)
        base["net_consumption"] = round(base["consumption_total"] - base["injection_total"], 4)
        return base

    def get_last_day_id(self) -> str | None:
        if self._data is None:
            return None
        return self._data.get("last_day")

    def get_day_metrics(self, day_id: str | None) -> dict[str, float]:
        if self._data is None or not day_id:
            return {metric: 0.0 for metric in LIFETIME_METRICS}
        return {
            metric: round(self._data["days"].get(day_id, {}).get(metric, 0.0), 4)
            for metric in LIFETIME_METRICS
        }
