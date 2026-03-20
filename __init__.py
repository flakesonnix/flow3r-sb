"""S&B Control — Control Storz & Bickel vaporizers from your flow3r badge.

Scans for nearby Venty, Veazy, Volcano Hybrid, or Crafty devices via BLE,
connects, and provides a touch-based UI for temperature control and heater
toggle.

Controls:
  - Top petals 0-4: adjust target temperature (-10, -5, +5, +10, toggle heater)
  - App button: scan/connect/reconnect
  - OS button: exit
"""

import json
import struct
import time

import bluetooth
import captouch
import leds
import st3m.run
from ctx import Context
from st3m.application import Application
from st3m.input import InputController

# ── S&B BLE UUIDs (from storz-rs) ──────────────────────────────────────────

# Volcano Hybrid
VOLCANO_SERVICE_STATE = bluetooth.UUID("10100000-5354-4f52-5a26-4249434b454c")
VOLCANO_SERVICE_CONTROL = bluetooth.UUID("10110000-5354-4f52-5a26-4249434b454c")
VOLCANO_CURRENT_TEMP = bluetooth.UUID("10110001-5354-4f52-5a26-4249434b454c")
VOLCANO_TARGET_TEMP = bluetooth.UUID("10110003-5354-4f52-5a26-4249434b454c")
VOLCANO_HEATER_ON = bluetooth.UUID("1011000f-5354-4f52-5a26-4249434b454c")
VOLCANO_HEATER_OFF = bluetooth.UUID("10110010-5354-4f52-5a26-4249434b454c")
VOLCANO_PUMP_ON = bluetooth.UUID("10110013-5354-4f52-5a26-4249434b454c")
VOLCANO_PUMP_OFF = bluetooth.UUID("10110014-5354-4f52-5a26-4249434b454c")
VOLCANO_ACTIVITY = bluetooth.UUID("1010000c-5354-4f52-5a26-4249434b454c")

# Venty / Veazy (shared protocol)
VENTY_SERVICE_PRIMARY = bluetooth.UUID("00000000-5354-4f52-5a26-4249434b454c")
VENTY_CONTROL = bluetooth.UUID("00000001-5354-4f52-5a26-4249434b454c")

# Crafty
CRAFTY_SERVICE_1 = bluetooth.UUID("00000001-4c45-4b43-4942-265a524f5453")
CRAFTY_WRITE_TEMP = bluetooth.UUID("00000021-4c45-4b43-4942-265a524f5453")
CRAFTY_HEATER_ON = bluetooth.UUID("00000081-4c45-4b43-4942-265a524f5453")
CRAFTY_HEATER_OFF = bluetooth.UUID("00000091-4c45-4b43-4942-265a524f5453")

# Device name prefixes
SB_PREFIXES = (b"STORZ&BICKEL", b"S&B VY", b"S&B VZ", b"S&B VOLCANO", b"S&B CRAFTY")

# Device type constants
DEV_NONE: int = const(0)
DEV_VOLCANO: int = const(1)
DEV_VENTY: int = const(2)
DEV_CRAFTY: int = const(3)

# Temperature range
TEMP_MIN: float = const(40.0)
TEMP_MAX: float = const(230.0)


class SBControl(Application):
    """Storz & Bickel vaporizer controller for flow3r."""

    def __init__(self, app_ctx):
        super().__init__(app_ctx)

        # BLE state
        self._ble = None
        self._conn_handle = None
        self._device_type = DEV_NONE
        self._device_name = ""
        self._scanning = False
        self._connected = False

        # Characteristic handles (Volcano)
        self._h_volcano_target: int | None = None
        self._h_volcano_heater_on: int | None = None
        self._h_volcano_heater_off: int | None = None
        self._h_volcano_current: int | None = None
        self._h_volcano_activity: int | None = None

        # Characteristic handles (Venty)
        self._h_venty_control: int | None = None

        # Characteristic handles (Crafty)
        self._h_crafty_target: int | None = None
        self._h_crafty_heater_on: int | None = None
        self._h_crafty_heater_off: int | None = None

        # Device state
        self.current_temp: float | None = None
        self.target_temp: float = 180.0
        self.heater_on: bool = False
        self.pump_on: bool = False
        self.battery: int | None = None

        # UI state
        self.status_msg: str = "Press App btn to scan"
        self.status_color: tuple = (0.5, 0.5, 0.5)
        self._last_think: int = 0
        self._led_timer: int = 0
        self._status_clear_at: int = 0

        # Scan results: list of (addr_type, addr_bytes, name, rssi)
        self._scan_results: list = []

    def get_help(self) -> str:
        return (
            "Control Storz & Bickel vaporizers via BLE.\n"
            "\n"
            "APP button: scan/connect/disconnect\n"
            "Top petals 0,2,4,6,8: temp -10, -5, on/off, +5, +10\n"
            "\n"
            "LED ring shows temp progress:\n"
            "  green = at target, orange = heating, blue = cooling\n"
            "\n"
            "Supports: Venty, Veazy, Volcano Hybrid, Crafty+"
        )

    def on_enter(self, vm):
        super().on_enter(vm)
        self._init_ble()

    def on_exit(self):
        super().on_exit()
        self._disconnect()
        if self._ble:
            self._ble.active(False)
            self._ble = None
        leds.set_all_rgb(0, 0, 0)
        leds.update()

    def _init_ble(self) -> None:
        """Initialize BLE radio."""
        try:
            self._ble = bluetooth.BLE()
            self._ble.active(True)
            self._ble.irq(self._ble_irq)
            self._set_status("BLE ready", (0, 0.8, 0))
        except Exception as e:
            self._set_status(f"BLE init err: {e}", (1, 0, 0))

    def _ble_irq(self, event, data):
        """BLE event handler."""
        if event == 5:  # _IRQ_SCAN_RESULT
            addr_type, addr, adv_type, rssi, adv_data = data
            name = self._parse_name(adv_data)
            if self._is_sb_device(name):
                addr_hex = ":".join("{:02x}".format(b) for b in addr)
                entry = (addr_type, bytes(addr), name, rssi)
                # Deduplicate
                for i, e in enumerate(self._scan_results):
                    if e[1] == bytes(addr):
                        self._scan_results[i] = entry
                        return
                self._scan_results.append(entry)
                self._set_status(
                    f"Found: {name.decode()[:15]} ({rssi}dBm)",
                    (0, 0.6, 1),
                )

        elif event == 6:  # _IRQ_SCAN_DONE
            self._scanning = False
            if self._scan_results:
                self._connect_best()
            else:
                self._set_status("No S&B devices found", (1, 0.5, 0))

        elif event == 7:  # _IRQ_PERIPHERAL_CONNECT
            conn_handle, addr_type, addr = data
            self._conn_handle = conn_handle
            self._connected = True
            self._set_status("Connected", (0, 1, 0))
            # Discover services
            self._ble.gattc_discover_services(conn_handle)

        elif event == 8:  # _IRQ_PERIPHERAL_DISCONNECT
            conn_handle, addr_type, addr = data
            self._conn_handle = None
            self._connected = False
            self._device_type = DEV_NONE
            self._set_status("Disconnected", (1, 0, 0))
            leds.set_all_rgb(1, 0, 0)
            leds.update()

        elif event == 9:  # _IRQ_GATTC_SERVICE_RESULT
            conn_handle, start_handle, end_handle, uuid = data
            self._discover_chars(conn_handle, start_handle, end_handle)

        elif event == 11:  # _IRQ_GATTC_CHARACTERISTIC_RESULT
            conn_handle, def_handle, value_handle, properties, uuid = data
            self._register_char(uuid, value_handle)

        elif event == 17:  # _IRQ_GATTC_NOTIFY
            conn_handle, value_handle, notify_data = data
            self._handle_notify(value_handle, notify_data)

        elif event == 18:  # _IRQ_GATTC_READ_RESULT
            conn_handle, value_handle, char_data = data
            self._handle_read(value_handle, char_data)

    def _parse_name(self, adv_data: bytes) -> bytes:
        """Extract local name from advertisement data."""
        i = 0
        while i < len(adv_data):
            length = adv_data[i]
            if length == 0:
                break
            type_ = adv_data[i + 1]
            if type_ == 0x09:  # Complete Local Name
                return bytes(adv_data[i + 2 : i + 1 + length])
            i += 1 + length
        return b""

    def _is_sb_device(self, name: bytes) -> bool:
        """Check if name matches S&B device prefixes."""
        if not name:
            return False
        for prefix in SB_PREFIXES:
            if name.startswith(prefix):
                return True
        return False

    def _start_scan(self) -> None:
        """Start BLE scanning for S&B devices."""
        if self._scanning:
            return
        self._scan_results = []
        self._scanning = True
        self._scan_start = time.ticks_ms()
        self._set_status("Scanning...", (0, 0.6, 1))
        try:
            self._ble.gap_scan(5000, 30000, 30000)
        except Exception as e:
            self._scanning = False
            self._set_status(f"Scan err: {e}", (1, 0, 0))

    def _connect_best(self) -> None:
        """Connect to the best (strongest RSSI) found device."""
        if not self._scan_results:
            return

        # Sort by RSSI (strongest first)
        self._scan_results.sort(key=lambda x: x[3], reverse=True)
        best = self._scan_results[0]
        addr_type, addr, name, rssi = best
        self._device_name = name.decode().strip("\x00")

        self._set_status(f"Connecting to {self._device_name}...", (0.8, 0.8, 0))
        try:
            self._ble.gap_connect(addr_type, addr, 5000)
        except Exception as e:
            self._set_status(f"Connect err: {e}", (1, 0, 0))

    def _disconnect(self) -> None:
        """Disconnect from current device."""
        if self._conn_handle is not None:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except Exception:
                pass
            self._conn_handle = None
            self._connected = False

    def _discover_chars(self, conn_handle: int, start_handle: int, end_handle: int) -> None:
        """Discover characteristics for a service."""
        try:
            self._ble.gattc_discover_characteristics(
                conn_handle, start_handle, end_handle
            )
        except Exception:
            pass

    def _register_char(self, uuid: bluetooth.UUID, value_handle: int) -> None:
        """Register a discovered characteristic handle."""
        if uuid == VOLCANO_CURRENT_TEMP:
            self._h_volcano_current = value_handle
            self._device_type = DEV_VOLCANO
            self._set_status("Volcano detected!", (0, 1, 0))
        elif uuid == VOLCANO_TARGET_TEMP:
            self._h_volcano_target = value_handle
        elif uuid == VOLCANO_HEATER_ON:
            self._h_volcano_heater_on = value_handle
        elif uuid == VOLCANO_HEATER_OFF:
            self._h_volcano_heater_off = value_handle
        elif uuid == VOLCANO_ACTIVITY:
            self._h_volcano_activity = value_handle
            self._subscribe_notify(value_handle)
        elif uuid == VENTY_CONTROL:
            self._h_venty_control = value_handle
            self._device_type = DEV_VENTY
            self._set_status("Venty/Veazy detected!", (0, 1, 0))
            self._subscribe_notify(value_handle)
            self._venty_init()
        elif uuid == CRAFTY_WRITE_TEMP:
            self._h_crafty_target = value_handle
            self._device_type = DEV_CRAFTY
            self._set_status("Crafty detected!", (0, 1, 0))
        elif uuid == CRAFTY_HEATER_ON:
            self._h_crafty_heater_on = value_handle
        elif uuid == CRAFTY_HEATER_OFF:
            self._h_crafty_heater_off = value_handle

    def _subscribe_notify(self, value_handle: int) -> None:
        """Subscribe to notifications on a characteristic."""
        if self._conn_handle is None:
            return
        try:
            # CCCD descriptor is at value_handle + 1
            cccd_handle = value_handle + 1
            self._ble.gattc_write(
                self._conn_handle, cccd_handle, struct.pack("<H", 0x0001), 1
            )
        except Exception:
            pass

    def _venty_init(self) -> None:
        """Send Venty/Veazy init sequence."""
        for cmd in (0x02, 0x1D, 0x01, 0x04):
            buf = bytearray(20)
            buf[0] = cmd
            self._write_venty(buf)

    def _handle_notify(self, value_handle: int, data: bytes) -> None:
        """Handle BLE notification."""
        if value_handle == self._h_volcano_activity and len(data) >= 2:
            flags = struct.unpack_from("<H", data, 0)[0]
            self.heater_on = bool(flags & 0x0020)
            self.pump_on = bool(flags & 0x2000)

        elif value_handle == self._h_volcano_current and len(data) >= 2:
            raw = struct.unpack_from("<H", data, 0)[0]
            self.current_temp = raw / 10.0

        elif value_handle == self._h_venty_control and len(data) >= 12:
            if data[0] == 0x01:
                # Target temp
                if len(data) >= 6:
                    raw = struct.unpack_from("<H", data, 4)[0]
                    self.target_temp = raw / 10.0
                # Battery
                if len(data) >= 9:
                    self.battery = data[8]
                # Heater mode
                if len(data) >= 12:
                    self.heater_on = data[11] > 0

    def _handle_read(self, value_handle: int, data: bytes) -> None:
        """Handle BLE read result."""
        if value_handle == self._h_volcano_current and len(data) >= 2:
            raw = struct.unpack_from("<H", data, 0)[0]
            self.current_temp = raw / 10.0

    # ── Device Control ──────────────────────────────────────────────────────

    def _set_target_temp(self, temp: float) -> None:
        """Set target temperature on device."""
        temp = max(40.0, min(230.0, temp))
        raw = int(temp * 10)

        if self._device_type == DEV_VOLCANO and self._h_volcano_target:
            data = struct.pack("<I", raw)
            self._write_char(self._h_volcano_target, data)

        elif self._device_type == DEV_VENTY and self._h_venty_control:
            buf = bytearray(20)
            buf[0] = 0x01  # Write cmd
            buf[1] = 0x02  # SET_TEMPERATURE mask
            buf[4] = raw & 0xFF
            buf[5] = (raw >> 8) & 0xFF
            self._write_venty(buf)

        elif self._device_type == DEV_CRAFTY and self._h_crafty_target:
            data = struct.pack("<H", raw)
            self._write_char(self._h_crafty_target, data)

        self.target_temp = temp

    def _heater_on(self) -> None:
        """Turn heater on."""
        if self._device_type == DEV_VOLCANO and self._h_volcano_heater_on:
            self._write_char(self._h_volcano_heater_on, b"\x00")
        elif self._device_type == DEV_VENTY and self._h_venty_control:
            buf = bytearray(20)
            buf[0] = 0x01
            buf[1] = 0x20  # HEATER mask
            buf[11] = 1  # Normal mode
            self._write_venty(buf)
        elif self._device_type == DEV_CRAFTY and self._h_crafty_heater_on:
            self._write_char(self._h_crafty_heater_on, b"\x00")
        self.heater_on = True

    def _heater_off(self) -> None:
        """Turn heater off."""
        if self._device_type == DEV_VOLCANO and self._h_volcano_heater_off:
            self._write_char(self._h_volcano_heater_off, b"\x00")
        elif self._device_type == DEV_VENTY and self._h_venty_control:
            buf = bytearray(20)
            buf[0] = 0x01
            buf[1] = 0x20  # HEATER mask
            buf[11] = 0  # Off
            self._write_venty(buf)
        elif self._device_type == DEV_CRAFTY and self._h_crafty_heater_off:
            self._write_char(self._h_crafty_heater_off, b"\x00")
        self.heater_on = False

    def _write_char(self, handle: int, data: bytes) -> None:
        """Write to a characteristic."""
        if self._conn_handle is None:
            return
        try:
            self._ble.gattc_write(self._conn_handle, handle, data, 1)
        except Exception:
            pass

    def _write_venty(self, data: bytearray) -> None:
        """Write to Venty control characteristic."""
        self._write_char(self._h_venty_control, data)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: tuple = (0.5, 0.5, 0.5)) -> None:
        """Set status message with color."""
        self.status_msg = msg
        self.status_color = color
        self._status_clear_at = time.ticks_add(time.ticks_ms(), 5000)

    def think(self, ins, delta_ms: int) -> None:
        super().think(ins, delta_ms)

        # App button: scan/connect
        if self.input.buttons.app.pressed:
            if not self._connected and not self._scanning:
                self._start_scan()
            elif self._connected:
                self._disconnect()
                self._set_status("Disconnected", (1, 0.5, 0))

        # Petal controls (top row: petals 0, 2, 4, 6, 8)
        if self._connected:
            petals = (0, 2, 4, 6, 8)
            for i, p in enumerate(petals):
                if self.input.captouch.petals[p].whole.pressed:
                    if i == 0:
                        self._set_target_temp(self.target_temp - 10)
                    elif i == 1:
                        self._set_target_temp(self.target_temp - 5)
                    elif i == 2:
                        if self.heater_on:
                            self._heater_off()
                        else:
                            self._heater_on()
                    elif i == 3:
                        self._set_target_temp(self.target_temp + 5)
                    elif i == 4:
                        self._set_target_temp(self.target_temp + 10)

        # LED feedback
        self._led_timer += delta_ms
        if self._led_timer > 100:
            self._led_timer = 0
            self._update_leds()

        # Clear old status
        if (
            self._status_clear_at
            and time.ticks_diff(time.ticks_ms(), self._status_clear_at) > 0
        ):
            if self._connected:
                self.status_msg = ""
            self._status_clear_at = 0

    def _update_leds(self) -> None:
        """Update LED ring based on state."""
        if not self._connected:
            # Breathing blue while scanning
            if self._scanning:
                t = (time.ticks_ms() % 2000) / 2000.0
                b = 0.3 + 0.3 * (0.5 + 0.5 * __import__("math").sin(t * 6.28))
                leds.set_all_rgb(0, 0, b)
            else:
                leds.set_all_rgb(0.05, 0.05, 0.05)
            leds.update()
            return

        # Connected: show temp progress as LED ring
        if self.current_temp is not None and self.target_temp > 0:
            ratio = min(1.0, self.current_temp / self.target_temp)
            num_on = int(ratio * 40)
            for i in range(40):
                if i < num_on:
                    if self.heater_on:
                        # Orange to red gradient
                        r = 1.0
                        g = max(0, 0.5 - ratio * 0.5)
                        leds.set_rgb(i, int(r * 60), int(g * 60), 0)
                    else:
                        # Blue when cooling
                        leds.set_rgb(i, 0, 0, int(ratio * 40))
                else:
                    leds.set_rgb(i, 2, 2, 2)
        else:
            leds.set_all_rgb(5, 5, 5)
        leds.update()

    def draw(self, ctx: Context) -> None:
        # Background
        ctx.rgb(0.05, 0.05, 0.08).rectangle(-120, -120, 240, 240).fill()

        if not self._connected:
            self._draw_disconnected(ctx)
        else:
            self._draw_connected(ctx)

    def _draw_disconnected(self, ctx: Context) -> None:
        """Draw disconnected state."""
        # Title
        ctx.rgb(1, 1, 1)
        ctx.font_size = 24
        ctx.text_align = ctx.CENTER
        ctx.move_to(0, -60)
        ctx.text("S&B Control")

        # Device name or status
        ctx.rgb(*self.status_color)
        ctx.font_size = 16
        ctx.move_to(0, -20)
        ctx.text(self.status_msg)

        # Scan results
        if self._scan_results and not self._scanning:
            ctx.rgb(0.7, 0.7, 0.7)
            ctx.font_size = 12
            for i, (_, _, name, rssi) in enumerate(self._scan_results[:4]):
                ctx.move_to(0, 10 + i * 18)
                ctx.text(f"{name.decode()[:18]} ({rssi}dBm)")

        # Help
        ctx.rgb(0.3, 0.3, 0.3)
        ctx.font_size = 11
        ctx.move_to(0, 90)
        ctx.text("App btn = scan/connect")

    def _draw_connected(self, ctx: Context) -> None:
        """Draw connected state with temperature display."""
        device_names = {
            DEV_VOLCANO: "Volcano",
            DEV_VENTY: "Venty",
            DEV_CRAFTY: "Crafty",
        }
        dname = device_names.get(self._device_type, "Device")

        # Title bar
        ctx.rgb(0.1, 0.1, 0.15).rectangle(-120, -120, 240, 28).fill()
        ctx.rgb(1, 1, 1)
        ctx.font_size = 14
        ctx.text_align = ctx.LEFT
        ctx.move_to(-110, -102)
        ctx.text(f"S&B {dname}")

        # Battery
        if self.battery is not None:
            ctx.rgb(0, 0.8, 0.4)
            ctx.move_to(90, -102)
            ctx.text(f"{self.battery}%")

        # Current temperature (big)
        if self.current_temp is not None:
            # Color based on proximity to target
            if self.current_temp >= self.target_temp - 2:
                color = (0, 1, 0.3)  # Green: at temp
            elif self.heater_on:
                color = (1, 0.5, 0)  # Orange: heating
            else:
                color = (0.3, 0.5, 1)  # Blue: cooling

            ctx.rgb(*color)
            ctx.font_size = 56
            ctx.text_align = ctx.CENTER
            ctx.move_to(0, -20)
            ctx.text(f"{self.current_temp:.1f}")

            ctx.rgb(0.5, 0.5, 0.5)
            ctx.font_size = 14
            ctx.move_to(55, -35)
            ctx.text("°C")

        # Target temperature
        ctx.rgb(0.8, 0.8, 0.8)
        ctx.font_size = 18
        ctx.text_align = ctx.CENTER
        ctx.move_to(0, 25)
        ctx.text(f"Target: {self.target_temp:.0f}°C")

        # Heater status
        if self.heater_on:
            ctx.rgb(1, 0.3, 0)
            ctx.font_size = 16
            ctx.move_to(0, 55)
            ctx.text("HEATER ON")
        else:
            ctx.rgb(0.3, 0.3, 0.3)
            ctx.font_size = 14
            ctx.move_to(0, 55)
            ctx.text("Heater off")

        # Pump (Volcano only)
        if self._device_type == DEV_VOLCANO:
            if self.pump_on:
                ctx.rgb(0, 0.8, 0.8)
                ctx.move_to(0, 75)
                ctx.text("PUMP ON")

        # Petal guide
        ctx.rgb(0.25, 0.25, 0.25)
        ctx.font_size = 10
        ctx.text_align = ctx.CENTER
        ctx.move_to(0, 100)
        ctx.text("-10  -5  ON/OFF  +5  +10")

        # Status message
        if self.status_msg:
            ctx.rgb(*self.status_color)
            ctx.font_size = 12
            ctx.move_to(0, 110)
            ctx.text(self.status_msg[:30])


if __name__ == "__main__":
    st3m.run.run_app(SBControl, "/flash/apps/sb_control")
