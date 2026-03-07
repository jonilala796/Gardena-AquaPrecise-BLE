"""BLE helper for Gardena AquaPrecise."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    BATTERY_LEVEL_CHAR_UUID,
    CHAR_DURATION_UUID,
    CHAR_POWER_UUID,
    CHAR_TRIGGER_UUID,
    CONNECT_TIMEOUT,
    PAIRING_TIMEOUT,
    WRITE_RETRIES,
    WRITE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class AquaPreciseBleError(Exception):
    """Base BLE error."""


class AquaPrecisePairingError(AquaPreciseBleError):
    """Raised when pairing fails."""


@dataclass(slots=True)
class AquaPreciseBleDevice:
    """Low-level AquaPrecise BLE API."""

    hass: HomeAssistant
    address: str
    name: str | None = None

    def _get_ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def async_pair(self) -> bool:
        """Try HA pairing API first, then fallback to Bleak pairing."""
        ble_device = self._get_ble_device()
        if ble_device is None:
            _LOGGER.debug(
                "Pairing start failed for %s: no BLE device from HA bluetooth manager",
                self.address,
            )
            raise AquaPrecisePairingError(
                f"No BLE device found for {self.address}. Device in range and connectable?"
            )

        _LOGGER.debug(
            "Pairing start for %s (name=%s, details=%s)",
            self.address,
            self.name,
            getattr(ble_device, "details", None),
        )

        manager = None
        try:
            manager = bluetooth.async_get_bluetooth(self.hass)
            if inspect.isawaitable(manager):
                manager = await manager
        except Exception:  # pragma: no cover
            manager = None

        if manager is not None:
            _LOGGER.debug(
                "HA bluetooth manager available for %s: %s",
                self.address,
                type(manager).__name__,
            )
            paired = await self._try_home_assistant_pairing_api(manager, ble_device)
            if paired:
                _LOGGER.debug("Paired %s via Home Assistant Bluetooth API", self.address)
                return True

        _LOGGER.debug("Falling back to Bleak client pairing for %s", self.address)
        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                self.name or self.address,
                timeout=CONNECT_TIMEOUT,
            )
            paired = await asyncio.wait_for(client.pair(), timeout=PAIRING_TIMEOUT)
            _LOGGER.debug(
                "Bleak pair() raw result for %s: %r (%s)",
                self.address,
                paired,
                type(paired).__name__,
            )
            if paired is None:
                _LOGGER.debug(
                    "Bleak pair() returned None for %s; assuming pairing request succeeded",
                    self.address,
                )
                paired = True

            if not paired:
                _LOGGER.warning(
                    "Pairing returned False for %s. Verifying whether device can still be used without bonding.",
                    self.address,
                )
                if await self._async_verify_characteristics_available(client):
                    _LOGGER.warning(
                        "Pairing rejected for %s, but required GATT characteristics are available. Continuing.",
                        self.address,
                    )
                    return True
                raise AquaPrecisePairingError(
                    "Pairing was rejected by the device and required characteristics are unavailable. "
                    "Ensure pairing mode is active and phone app is disconnected."
                )
            return True
        except (TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Pairing timed out for %s: %s", self.address, err)
            raise AquaPrecisePairingError("Pairing timed out") from err
        except BleakError as err:
            if await self._async_can_continue_after_pair_exception(client, err):
                return True
            _LOGGER.warning("Bleak pairing error for %s: %s", self.address, err)
            raise AquaPrecisePairingError(str(err)) from err
        except Exception as err:
            _LOGGER.warning(
                "Unexpected pairing error for %s (%s): %s",
                self.address,
                type(err).__name__,
                err,
            )
            raise AquaPrecisePairingError(str(err)) from err
        finally:
            if client is not None and client.is_connected:
                await client.disconnect()
                _LOGGER.debug("Disconnected BLE client after pairing for %s", self.address)

    async def _async_can_continue_after_pair_exception(
        self, client: BleakClient | None, err: BleakError
    ) -> bool:
        """Return True if pairing exception can be tolerated for this device/backend."""
        if client is None or not client.is_connected:
            return False

        msg = str(err).lower()
        hints = (
            "not supported",
            "rejected",
            "failed",
            "pair",
            "auth",
        )
        if not any(hint in msg for hint in hints):
            return False

        _LOGGER.warning(
            "Pairing raised '%s' for %s. Checking if required characteristics are still accessible.",
            err,
            self.address,
        )
        ok = await self._async_verify_characteristics_available(client)
        if ok:
            _LOGGER.warning(
                "Proceeding without confirmed bond for %s because GATT characteristics are accessible.",
                self.address,
            )
        return ok

    async def _async_verify_characteristics_available(self, client: BleakClient) -> bool:
        """Verify mandatory AquaPrecise characteristics exist after connect/pair attempt."""
        try:
            services = client.services
            if services is None:
                services = await client.get_services()

            available = {
                char.uuid.lower()
                for service in services
                for char in service.characteristics
            }
            required = {
                CHAR_DURATION_UUID,
                CHAR_TRIGGER_UUID,
                CHAR_POWER_UUID,
            }
            missing = required - available

            if missing:
                _LOGGER.debug(
                    "Required characteristics missing for %s: %s",
                    self.address,
                    sorted(missing),
                )
                return False

            _LOGGER.debug(
                "All required characteristics available for %s after pair attempt",
                self.address,
            )
            return True
        except Exception as probe_err:
            _LOGGER.debug(
                "Characteristic probe failed for %s: %s",
                self.address,
                probe_err,
            )
            return False

    async def _try_home_assistant_pairing_api(
        self, manager: Any, ble_device: BLEDevice
    ) -> bool:
        """Call the best available HA pairing helper dynamically."""
        candidates: tuple[tuple[str, tuple[Any, ...]], ...] = (
            ("async_pair_device", (ble_device,)),
            ("async_pair_device", (self.address,)),
            ("async_pair", (ble_device,)),
            ("async_pair", (self.address,)),
            ("async_pair_ble_device", (ble_device,)),
            ("async_pair_ble_device", (self.address,)),
        )

        for method_name, args in candidates:
            method = getattr(manager, method_name, None)
            if method is None:
                continue
            try:
                _LOGGER.debug(
                    "Trying HA pairing method %s for %s", method_name, self.address
                )
                result = method(*args)
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(result, timeout=PAIRING_TIMEOUT)
                _LOGGER.debug(
                    "HA pairing method %s result for %s: %r (%s)",
                    method_name,
                    self.address,
                    result,
                    type(result).__name__,
                )
                if result is None:
                    _LOGGER.debug(
                        "HA pairing method %s returned None for %s; assuming success",
                        method_name,
                        self.address,
                    )
                    return True
                return bool(result)
            except TypeError:
                _LOGGER.debug(
                    "HA pairing method %s had incompatible signature for %s",
                    method_name,
                    self.address,
                )
                continue
            except Exception as err:
                _LOGGER.debug(
                    "HA pairing method %s failed for %s: %s",
                    method_name,
                    self.address,
                    err,
                )
                continue

        _LOGGER.debug("No HA pairing method succeeded for %s", self.address)
        return False

    async def async_start_watering(self, seconds: int) -> None:
        """Run start sequence: duration, trigger, power=1."""
        duration_payload = int(seconds).to_bytes(4, byteorder="little", signed=False)
        await self._async_write_with_retry(
            (
                (CHAR_DURATION_UUID, duration_payload),
                (CHAR_TRIGGER_UUID, b"\x01"),
                (CHAR_POWER_UUID, b"\x01"),
            )
        )

    async def async_stop_watering(self) -> None:
        """Stop watering by writing power=0."""
        await self._async_write_with_retry(((CHAR_POWER_UUID, b"\x00"),))

    async def async_read_battery(self) -> int | None:
        """Read battery level if available."""
        ble_device = self._get_ble_device()
        if ble_device is None:
            _LOGGER.debug("Cannot read battery: no BLE device for %s", self.address)
            return None

        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                self.name or self.address,
                timeout=CONNECT_TIMEOUT,
            )
            raw = await asyncio.wait_for(
                client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID),
                timeout=WRITE_TIMEOUT,
            )
            if not raw:
                return None
            return int(raw[0])
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Battery read failed for %s: %s", self.address, err)
            return None
        finally:
            if client is not None and client.is_connected:
                await client.disconnect()

    async def async_read_power_state(self) -> bool | None:
        """Read current watering power state from power characteristic."""
        ble_device = self._get_ble_device()
        if ble_device is None:
            _LOGGER.debug("Cannot read power state: no BLE device for %s", self.address)
            return None

        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                self.name or self.address,
                timeout=CONNECT_TIMEOUT,
            )
            raw = await asyncio.wait_for(
                client.read_gatt_char(CHAR_POWER_UUID),
                timeout=WRITE_TIMEOUT,
            )
            if not raw:
                return None
            state = bytes(raw)[0] == 0x01
            _LOGGER.debug(
                "Read power state for %s: raw=%s -> is_on=%s",
                self.address,
                bytes(raw).hex(),
                state,
            )
            return state
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Power read failed for %s: %s", self.address, err)
            return None
        finally:
            if client is not None and client.is_connected:
                await client.disconnect()

    async def async_listen_power_notifications(
        self,
        on_state: Callable[[bool], None],
        stop_event: asyncio.Event,
        on_battery: Callable[[int], None] | None = None,
    ) -> None:
        """Listen to power notifications until stop_event is set with reconnect/resubscribe."""
        _LOGGER.debug("Starting power notification loop for %s", self.address)

        while not stop_event.is_set():
            ble_device = self._get_ble_device()
            if ble_device is None:
                _LOGGER.debug(
                    "Power notification loop: no BLE device for %s, retrying",
                    self.address,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
                except TimeoutError:
                    pass
                continue

            disconnected_event = asyncio.Event()
            client: BleakClient | None = None
            power_notify_started = False
            battery_notify_started = False

            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.name or self.address,
                    timeout=CONNECT_TIMEOUT,
                )

                def _on_disconnect(_client: BleakClient) -> None:
                    _LOGGER.debug("Power notify disconnected for %s", self.address)
                    self.hass.loop.call_soon_threadsafe(disconnected_event.set)

                client.set_disconnected_callback(_on_disconnect)

                def _notification_handler(_sender: Any, data: bytearray) -> None:
                    if not data:
                        _LOGGER.debug("Power notification empty for %s", self.address)
                        return
                    payload = bytes(data)
                    if payload[0] == 0x01:
                        is_on = True
                    elif payload[0] == 0x00:
                        is_on = False
                    else:
                        _LOGGER.debug(
                            "Power notification unsupported payload for %s: %s",
                            self.address,
                            payload.hex(),
                        )
                        return

                    _LOGGER.debug(
                        "Power notification for %s: raw=%s -> is_on=%s",
                        self.address,
                        payload.hex(),
                        is_on,
                    )
                    self.hass.loop.call_soon_threadsafe(on_state, is_on)

                await asyncio.wait_for(
                    client.start_notify(CHAR_POWER_UUID, _notification_handler),
                    timeout=WRITE_TIMEOUT,
                )
                power_notify_started = True
                _LOGGER.debug("Power notify subscribed for %s", self.address)

                if on_battery is not None:
                    def _battery_notification_handler(_sender: Any, data: bytearray) -> None:
                        if not data:
                            _LOGGER.debug(
                                "Battery notification empty for %s", self.address
                            )
                            return
                        battery = int(bytes(data)[0])
                        _LOGGER.debug(
                            "Battery notification for %s: raw=%s -> battery=%s%%",
                            self.address,
                            bytes(data).hex(),
                            battery,
                        )
                        self.hass.loop.call_soon_threadsafe(on_battery, battery)

                    try:
                        await asyncio.wait_for(
                            client.start_notify(
                                BATTERY_LEVEL_CHAR_UUID, _battery_notification_handler
                            ),
                            timeout=WRITE_TIMEOUT,
                        )
                        battery_notify_started = True
                        _LOGGER.debug("Battery notify subscribed for %s", self.address)
                    except Exception as err:
                        _LOGGER.debug(
                            "Battery notify subscribe failed for %s: %s",
                            self.address,
                            err,
                        )

                initial = await asyncio.wait_for(
                    client.read_gatt_char(CHAR_POWER_UUID),
                    timeout=WRITE_TIMEOUT,
                )
                if initial:
                    current = bytes(initial)[0] == 0x01
                    self.hass.loop.call_soon_threadsafe(on_state, current)

                if on_battery is not None:
                    try:
                        initial_battery = await asyncio.wait_for(
                            client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID),
                            timeout=WRITE_TIMEOUT,
                        )
                        if initial_battery:
                            self.hass.loop.call_soon_threadsafe(
                                on_battery, int(bytes(initial_battery)[0])
                            )
                    except Exception as err:
                        _LOGGER.debug(
                            "Initial battery read failed for %s: %s",
                            self.address,
                            err,
                        )

                stop_wait = asyncio.create_task(stop_event.wait())
                disconnect_wait = asyncio.create_task(disconnected_event.wait())
                done, pending = await asyncio.wait(
                    {stop_wait, disconnect_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    if task is disconnect_wait and disconnected_event.is_set():
                        _LOGGER.debug(
                            "Power notify reconnect needed for %s",
                            self.address,
                        )
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug(
                    "Power notification loop error for %s: %s",
                    self.address,
                    err,
                )
            finally:
                if client is not None and client.is_connected:
                    try:
                        if power_notify_started:
                            await asyncio.wait_for(
                                client.stop_notify(CHAR_POWER_UUID),
                                timeout=WRITE_TIMEOUT,
                            )
                    except Exception:
                        pass
                    try:
                        if battery_notify_started:
                            await asyncio.wait_for(
                                client.stop_notify(BATTERY_LEVEL_CHAR_UUID),
                                timeout=WRITE_TIMEOUT,
                            )
                    except Exception:
                        pass
                    await client.disconnect()

            if not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass

        _LOGGER.debug("Power notification loop stopped for %s", self.address)

    async def _async_write_with_retry(
        self, writes: tuple[tuple[str, bytes], ...]
    ) -> None:
        """Perform write sequence with reconnect retries."""
        attempts = WRITE_RETRIES + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self._async_write_sequence(writes)
                return
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                last_error = err
                _LOGGER.warning(
                    "BLE write attempt %s/%s failed for %s: %s",
                    attempt,
                    attempts,
                    self.address,
                    err,
                )
                await asyncio.sleep(0.6)

        raise AquaPreciseBleError(
            f"BLE write failed after {attempts} attempts: {last_error}"
        )

    async def _async_write_sequence(self, writes: tuple[tuple[str, bytes], ...]) -> None:
        ble_device = self._get_ble_device()
        if ble_device is None:
            raise AquaPreciseBleError(
                f"No BLE device for {self.address}. Is Bluetooth proxy in range?"
            )

        client: BleakClient | None = None
        try:
            _LOGGER.debug("Connecting to AquaPrecise %s", self.address)
            client = await establish_connection(
                BleakClient,
                ble_device,
                self.name or self.address,
                timeout=CONNECT_TIMEOUT,
            )

            for char_uuid, payload in writes:
                _LOGGER.debug(
                    "Write %s -> %s on %s", payload.hex(), char_uuid, self.address
                )
                await asyncio.wait_for(
                    client.write_gatt_char(char_uuid, payload, response=True),
                    timeout=WRITE_TIMEOUT,
                )
        finally:
            if client is not None and client.is_connected:
                await client.disconnect()
                _LOGGER.debug("Disconnected from AquaPrecise %s", self.address)
