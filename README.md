# Gardena AquaPrecise BLE (Home Assistant Custom Integration)

HACS-ready custom integration with domain `gardena_aquaprecise_ble` to control Gardena AquaPrecise via Bluetooth LE.

## Features

- Bluetooth advertisement discovery in config flow (`async_step_bluetooth` + `async_step_user`)
- Candidate scoring by:
  - `connectable == true`
  - name contains `AquaPrecise`
  - service UUID `98bd0001-0b0e-421a-84e5-ddbf75dc6de4`
  - manufacturer data contains ID `1062`
- Bluetooth Proxy support (ESPHome) by using Home Assistant Bluetooth discovery APIs
- Pairing/bonding initiated by the integration during flow and setup
- GATT control by UUID only:
  - duration write (`uint32`, little-endian)
  - trigger write (`01`)
  - power write (`01`/`00`)
- Entities:
  - `switch.aquaprecise_watering`
  - `number.aquaprecise_duration_seconds`
  - optional `sensor.aquaprecise_battery`
- Services:
  - `gardena_aquaprecise_ble.start_watering` (with `seconds`)
  - `gardena_aquaprecise_ble.stop_watering`

## Discovery

When the device is in range (local adapter or Bluetooth Proxy), Home Assistant proposes AquaPrecise candidates automatically in the config flow. If multiple devices are discovered, select one from the dropdown.

## Pairing/Bonding

The integration starts pairing itself.

If pairing fails:

1. Put AquaPrecise into pairing mode (press/hold button until LED blinks).
2. Disconnect the Gardena phone app (device can usually handle only one active connection).
3. Click retry in the pairing step.

Bond persistence/trust is handled by the host Bluetooth stack (BlueZ / OS).

## GATT protocol used

- Primary matching service UUID: `98bd0001-0b0e-421a-84e5-ddbf75dc6de4`
- Power characteristic: `98bd0d11-0b0e-421a-84e5-ddbf75dc6de4`
  - Start: `01`
  - Stop: `00`
- Duration characteristic: `98bd0d13-0b0e-421a-84e5-ddbf75dc6de4`
  - Example 60 seconds: `3c 00 00 00`
- Trigger characteristic: `98bd0a17-0b0e-421a-84e5-ddbf75dc6de4`
  - Write `01` before start

Start sequence:

1. Write duration
2. Write trigger
3. Write power `01`

Stop:

1. Write power `00`

## Services usage examples

```yaml
service: gardena_aquaprecise_ble.start_watering
data:
  seconds: 120
```

```yaml
service: gardena_aquaprecise_ble.stop_watering
```

Optional targeting:

- `entry_id`
- `entity_id`

## Installation

1. Copy this repository into your Home Assistant custom repositories (HACS) and install it.
2. Restart Home Assistant.
3. Add integration: **Gardena AquaPrecise BLE**.
4. Select discovered device and complete pairing.

## Notes

- BLE commands connect on-demand per action and disconnect afterwards to reduce conflicts.
- Write path has timeout and retries.
- If device control fails intermittently, verify RSSI/proxy placement and that the mobile app is not connected.
