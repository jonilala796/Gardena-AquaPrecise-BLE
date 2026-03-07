"""The Gardena AquaPrecise BLE integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.components import bluetooth
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .ble import AquaPreciseBleDevice, AquaPrecisePairingError
from .const import (
    CONF_ADDRESS,
    CONF_DURATION_SECONDS,
    CONF_NAME,
    CONF_PAIRED,
    DEFAULT_DURATION_SECONDS,
    DOMAIN,
    PLATFORMS,
    SERVICE_START_WATERING,
    SERVICE_STOP_WATERING,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class AquaPreciseRuntime:
    """Runtime data for one config entry."""

    ble_device: AquaPreciseBleDevice
    duration_seconds: int
    is_watering: bool = False
    battery_level: int | None = None
    _listeners: list[Callable[[], None]] = field(default_factory=list)
    _command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _notify_stop_event: asyncio.Event | None = None
    _notify_task: asyncio.Task[None] | None = None

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Register a listener for state updates."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        """Remove listener for state updates."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _set_watering_state(self, value: bool) -> None:
        if self.is_watering == value:
            return
        self.is_watering = value
        self._notify_listeners()

    def mark_stopped(self) -> None:
        """Mark watering as stopped."""
        self._set_watering_state(False)

    def mark_started(self, seconds: int) -> None:
        """Mark watering as started."""
        self.duration_seconds = seconds
        self._set_watering_state(True)

    @callback
    def _handle_power_notification(self, is_on: bool) -> None:
        """Apply state from BLE power notification."""
        _LOGGER.debug(
            "Applying power notification for %s: is_on=%s",
            self.ble_device.address,
            is_on,
        )
        self._set_watering_state(is_on)

    @callback
    def _handle_battery_notification(self, battery: int) -> None:
        """Apply battery value from BLE notification/read."""
        value = max(0, min(100, int(battery)))
        if self.battery_level == value:
            return
        _LOGGER.debug(
            "Applying battery update for %s: %s%%",
            self.ble_device.address,
            value,
        )
        self.battery_level = value
        self._notify_listeners()

    def _request_stop_notifications(self) -> None:
        if self._notify_stop_event is not None:
            self._notify_stop_event.set()

    async def _async_stop_notifications(self) -> None:
        self._request_stop_notifications()
        if self._notify_task is not None:
            task = self._notify_task
            self._notify_task = None
            try:
                await task
            except Exception as err:
                _LOGGER.debug(
                    "Notification task ended with error for %s: %s",
                    self.ble_device.address,
                    err,
                )
        self._notify_stop_event = None

    async def _async_start_notifications(self) -> None:
        if self._notify_task is not None and not self._notify_task.done():
            return

        await self._async_stop_notifications()
        stop_event = asyncio.Event()
        self._notify_stop_event = stop_event

        async def _runner() -> None:
            try:
                await self.ble_device.async_listen_power_notifications(
                    self._handle_power_notification,
                    stop_event,
                    self._handle_battery_notification,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Power notification listener failed for %s: %s",
                    self.ble_device.address,
                    err,
                )
            finally:
                if self._notify_stop_event is stop_event:
                    self._notify_stop_event = None
                self._notify_task = None

        self._notify_task = asyncio.create_task(_runner())

    async def async_start_notifications(self) -> None:
        """Ensure power notification listener is running."""
        await self._async_start_notifications()

    async def async_start_watering(self, seconds: int) -> None:
        """Start watering and begin listening for power notifications."""
        async with self._command_lock:
            await self.ble_device.async_start_watering(seconds)
            self.duration_seconds = seconds
            await self._async_start_notifications()

        async def _sync_state() -> None:
            state = await self.ble_device.async_read_power_state()
            if state is not None:
                self._handle_power_notification(state)

        asyncio.create_task(_sync_state())

    async def async_stop_watering(self) -> None:
        """Stop watering and sync state from device notifications/readback."""
        async with self._command_lock:
            await self.ble_device.async_stop_watering()

        async def _sync_state() -> None:
            state = await self.ble_device.async_read_power_state()
            if state is not None:
                self._handle_power_notification(state)

        asyncio.create_task(_sync_state())

    async def async_shutdown(self) -> None:
        """Stop background tasks on unload."""
        await self._async_stop_notifications()
        self.mark_stopped()


type AquaPreciseConfigEntry = ConfigEntry[AquaPreciseRuntime]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration from YAML (not used)."""
    hass.data.setdefault(DOMAIN, {})

    async def _resolve_runtime(call: ServiceCall) -> list[AquaPreciseRuntime]:
        entry_id = call.data.get("entry_id")
        entity_id = call.data.get(ATTR_ENTITY_ID)

        entries = hass.config_entries.async_entries(DOMAIN)
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]

        if entity_id:
            entity_registry = er.async_get(hass)
            entity_ids = [entity_id] if isinstance(entity_id, str) else entity_id
            allowed_entry_ids = {
                entity_registry.async_get(eid).config_entry_id
                for eid in entity_ids
                if entity_registry.async_get(eid) is not None
            }
            entries = [entry for entry in entries if entry.entry_id in allowed_entry_ids]

        runtimes: list[AquaPreciseRuntime] = []
        for entry in entries:
            runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if runtime is not None:
                runtimes.append(runtime)
        return runtimes

    async def _service_start(call: ServiceCall) -> None:
        runtimes = await _resolve_runtime(call)
        seconds = call.data.get("seconds")

        for runtime in runtimes:
            desired = int(seconds) if seconds is not None else runtime.duration_seconds
            await runtime.async_start_watering(desired)

    async def _service_stop(call: ServiceCall) -> None:
        runtimes = await _resolve_runtime(call)
        for runtime in runtimes:
            await runtime.async_stop_watering()

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_WATERING,
        _service_start,
        schema=vol.Schema(
            {
                vol.Optional("seconds"): vol.Coerce(int),
                vol.Optional("entry_id"): str,
                vol.Optional(ATTR_ENTITY_ID): vol.Any(str, [str]),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_WATERING,
        _service_stop,
        schema=vol.Schema(
            {
                vol.Optional("entry_id"): str,
                vol.Optional(ATTR_ENTITY_ID): vol.Any(str, [str]),
            }
        ),
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: AquaPreciseConfigEntry) -> bool:
    """Set up Gardena AquaPrecise BLE from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if bluetooth.async_scanner_count(hass, connectable=True) == 0:
        raise ConfigEntryNotReady(
            "No connectable Bluetooth scanner available (local adapter or proxy)."
        )

    address = entry.data[CONF_ADDRESS]
    name = entry.data.get(CONF_NAME)
    paired = bool(entry.data.get(CONF_PAIRED, False))

    ble_device = AquaPreciseBleDevice(hass, address=address, name=name)

    if not paired:
        try:
            _LOGGER.debug("Entry %s: trying setup-time pairing for %s", entry.entry_id, address)
            paired = await ble_device.async_pair()
        except AquaPrecisePairingError as err:
            _LOGGER.warning(
                "Pairing failed during setup for %s. Device might be connected by mobile app: %s",
                address,
                err,
            )
            raise ConfigEntryNotReady(
                "Pairing failed. Put AquaPrecise in pairing mode and ensure app is disconnected."
            ) from err

        if paired:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_PAIRED: True},
            )

    runtime = AquaPreciseRuntime(
        ble_device=ble_device,
        duration_seconds=int(entry.data.get(CONF_DURATION_SECONDS, DEFAULT_DURATION_SECONDS)),
    )

    hass.data[DOMAIN][entry.entry_id] = runtime
    await runtime.async_start_notifications()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AquaPreciseConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = hass.data[DOMAIN].pop(entry.entry_id, None)
        if runtime is not None:
            await runtime.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AquaPreciseConfigEntry) -> None:
    """Handle removal of a config entry."""
    address = entry.data.get(CONF_ADDRESS)
    if address:
        bluetooth.async_rediscover_address(hass, address)
