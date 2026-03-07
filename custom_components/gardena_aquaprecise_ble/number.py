"""Number platform for Gardena AquaPrecise BLE."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AquaPreciseRuntime
from .const import (
    CONF_DURATION_SECONDS,
    DEFAULT_DURATION_SECONDS,
    DOMAIN,
    DURATION_STEP_SECONDS,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: AquaPreciseRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AquaPreciseDurationNumber(hass, entry, runtime)])


class AquaPreciseDurationNumber(NumberEntity):
    """Target duration number entity."""

    _attr_has_entity_name = True
    _attr_name = "Duration Seconds"
    _attr_native_min_value = MIN_DURATION_SECONDS
    _attr_native_max_value = MAX_DURATION_SECONDS
    _attr_native_step = DURATION_STEP_SECONDS
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, runtime: AquaPreciseRuntime
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_duration_seconds"
        if self._runtime.duration_seconds <= 0:
            self._runtime.duration_seconds = DEFAULT_DURATION_SECONDS

    @property
    def native_value(self) -> float:
        return float(self._runtime.duration_seconds)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "Gardena",
            "model": "AquaPrecise",
        }

    async def async_set_native_value(self, value: float) -> None:
        new_value = int(value)
        self._runtime.duration_seconds = new_value
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_DURATION_SECONDS: new_value},
        )
        self.async_write_ha_state()
