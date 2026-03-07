"""Sensor platform for Gardena AquaPrecise BLE."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AquaPreciseRuntime
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: AquaPreciseRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AquaPreciseBatterySensor(entry, runtime)])


class AquaPreciseBatterySensor(SensorEntity):
    """Battery sensor from standard battery characteristic."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: AquaPreciseRuntime) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_battery"

    async def async_added_to_hass(self) -> None:
        """Register runtime listener when entity is added."""
        await super().async_added_to_hass()
        self._runtime.add_listener(self._handle_runtime_update)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Remove runtime listener."""
        self._runtime.remove_listener(self._handle_runtime_update)
        await super().async_will_remove_from_hass()

    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._runtime.battery_level

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "Gardena",
            "model": "AquaPrecise",
        }
