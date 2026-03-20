# flow3r-sb

**Control your Storz & Bickel vaporizer from a flow3r badge.**

A MicroPython app for the [flow3r](https://docs.flow3r.garden/) conference badge
that connects to Storz & Bickel vaporizers via BLE and provides a touch-based UI
for temperature control.

## Supported Devices

- Venty
- Veazy
- Volcano Hybrid
- Crafty+

## Installation

1. Connect your flow3r badge via USB (Disk Mode)
2. Copy this folder to `apps/sb_control` on the badge
3. Restart the badge
4. Find "S&B Control" in the Apps menu

## Controls

![Controls Diagram](assets/controls.svg)

| Control | Action |
|---------|--------|
| Petal 0 | Target temp -10°C |
| Petal 1 | Target temp -5°C |
| Petal 2 | Toggle heater on/off |
| Petal 3 | Target temp +5°C |
| Petal 4 | Target temp +10°C |
| App button | Scan / connect / disconnect |
| OS button | Exit |

## Connection

![Connection Flow](assets/flow.svg)

1. Open "S&B Control" from Apps menu
2. Press APP button to start BLE scan
3. LED ring breathes blue while scanning
4. App auto-connects to strongest S&B device found
5. Use petals to control temperature

## Display

![Screen States](assets/screens.svg)

## Display

- **Current temperature** — large number in center
- **Target temperature** — below current
- **Heater status** — ON (orange) / off (gray)
- **Pump status** — ON (Volcano only)
- **Battery** — top right (Venty/Veazy)
- **LED ring** — temp progress (orange when heating, blue when cooling)

## Requirements

- flow3r badge with firmware v1.4.0+
- Storz & Bickel device powered on and in BLE range

## Protocol

Uses the same BLE protocol as [storz-rs](https://github.com/flakesonnix/storz-rs),
reverse-engineered from the official app.
