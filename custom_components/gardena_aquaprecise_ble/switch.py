"""Switch platform for Gardena AquaPrecise BLE."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities([AquaPreciseWateringSwitch(entry, runtime)])


class AquaPreciseWateringSwitch(SwitchEntity):
    """AquaPrecise watering switch."""

    _attr_has_entity_name = True
    _attr_name = "Watering"

    def __init__(self, entry: ConfigEntry, runtime: AquaPreciseRuntime) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_watering"

    async def async_added_to_hass(self) -> None:
        """Register runtime listener when entity is added."""
        await super().async_added_to_hass()
        self._runtime.add_listener(self._handle_runtime_update)

    async def async_will_remove_from_hass(self) -> None:
        """Remove runtime listener."""
        self._runtime.remove_listener(self._handle_runtime_update)
        await super().async_will_remove_from_hass()

    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._runtime.is_watering

    @property
    def available(self) -> bool:
        return True

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "Gardena",
            "model": "AquaPrecise",
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._runtime.async_start_watering(self._runtime.duration_seconds)

    async def async_turn_off(self, **kwargs) -> None:
        await self._runtime.async_stop_watering()
