"""Config flow for Gardena AquaPrecise BLE integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .ble import AquaPreciseBleDevice, AquaPrecisePairingError
from .const import (
    CONF_DURATION_MINUTES,
    CONF_DURATION_SECONDS,
    CONF_PAIRED,
    DEFAULT_DURATION_MINUTES,
    MAX_DURATION_MINUTES,
    MIN_DURATION_MINUTES,
    DOMAIN,
    GARDENA_MANUFACTURER_ID,
    SERVICE_UUID_MATCH,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscoveryCandidate:
    """Discovered BLE candidate."""

    address: str
    name: str
    rssi: int | None
    score: int
    source: str | None

    def label(self) -> str:
        rssi = self.rssi if self.rssi is not None else "n/a"
        source = f" | source: {self.source}" if self.source else ""
        return f"{self.name} ({self.address}) RSSI: {rssi} | score: {self.score}{source}"


class GardenaAquapreciseFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Gardena AquaPrecise BLE."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get options flow for this handler."""
        return GardenaAquapreciseOptionsFlowHandler(config_entry)

    def __init__(self) -> None:
        self._candidates: dict[str, DiscoveryCandidate] = {}
        self._selected_address: str | None = None
        self._selected_name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> config_entries.ConfigFlowResult:
        """Handle discovery from bluetooth integration."""
        self._upsert_candidate(discovery_info)
        await self.async_set_unique_id(discovery_info.address.lower())
        self._abort_if_unique_id_configured()

        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle manual step and discovered devices list."""
        self._collect_discovered_candidates()

        if not self._candidates:
            return self.async_abort(reason="no_devices_found")

        options = [
            selector.SelectOptionDict(value=address, label=candidate.label())
            for address, candidate in sorted(
                self._candidates.items(), key=lambda item: item[1].score, reverse=True
            )
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            candidate = self._candidates[address]
            if (
                bluetooth.async_ble_device_from_address(
                    self.hass, candidate.address, connectable=True
                )
                is None
            ):
                return self.async_show_form(
                    step_id="user",
                    data_schema=schema,
                    errors={"base": "not_connectable"},
                )
            self._selected_address = candidate.address
            self._selected_name = candidate.name
            await self.async_set_unique_id(candidate.address.lower())
            self._abort_if_unique_id_configured()
            return await self.async_step_pair()

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_pair(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Attempt pairing and create entry on success."""
        if self._selected_address is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is not None:
            # Retry requested
            pass

        ble_device = AquaPreciseBleDevice(
            self.hass,
            address=self._selected_address,
            name=self._selected_name,
        )

        try:
            paired = await ble_device.async_pair()
        except AquaPrecisePairingError as err:
            _LOGGER.warning(
                "Pairing failed for %s: %s. Is the phone app connected?",
                self._selected_address,
                err,
            )
            errors["base"] = "pairing_failed"
            paired = False

        if paired:
            title = self._selected_name or f"AquaPrecise {self._selected_address}"
            return self.async_create_entry(
                title=title,
                data={
                    CONF_ADDRESS: self._selected_address,
                    CONF_NAME: self._selected_name or "AquaPrecise",
                    CONF_PAIRED: True,
                },
            )

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "device": self._selected_name or self._selected_address,
            },
        )

    def _collect_discovered_candidates(self) -> None:
        for service_info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            if not isinstance(service_info, BluetoothServiceInfoBleak):
                continue
            self._upsert_candidate(service_info)

    def _upsert_candidate(self, service_info: BluetoothServiceInfoBleak) -> None:
        score = _score_candidate(service_info)
        if score <= 0:
            return

        name = service_info.name or service_info.device.name or "Unknown BLE Device"
        candidate = DiscoveryCandidate(
            address=service_info.address,
            name=name,
            rssi=service_info.rssi,
            score=score,
            source=getattr(service_info, "source", None),
        )

        existing = self._candidates.get(candidate.address)
        if existing is None or candidate.score >= existing.score:
            self._candidates[candidate.address] = candidate


def _score_candidate(service_info: BluetoothServiceInfoBleak) -> int:
    score = 0

    if service_info.connectable:
        score += 3

    name = (service_info.name or service_info.device.name or "").lower()
    if "aquaprecise" in name:
        score += 4

    service_uuids = {uuid.lower() for uuid in (service_info.service_uuids or [])}
    if SERVICE_UUID_MATCH in service_uuids:
        score += 5

    manufacturer_data = service_info.manufacturer_data or {}
    if GARDENA_MANUFACTURER_ID in manufacturer_data:
        score += 2

    return score


class GardenaAquapreciseOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for default watering duration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, int] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        legacy_seconds = int(
            self._config_entry.options.get(
                CONF_DURATION_SECONDS,
                self._config_entry.data.get(
                    CONF_DURATION_SECONDS,
                    DEFAULT_DURATION_MINUTES * 60,
                ),
            )
        )
        fallback_minutes = max(1, (legacy_seconds + 59) // 60)

        current_duration_minutes = max(
            MIN_DURATION_MINUTES,
            min(
                MAX_DURATION_MINUTES,
                int(
                    self._config_entry.options.get(
                        CONF_DURATION_MINUTES,
                        fallback_minutes,
                    )
                ),
            ),
        )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_DURATION_MINUTES, default=current_duration_minutes): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_DURATION_MINUTES, max=MAX_DURATION_MINUTES),
                    cv.positive_int,
                )
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
