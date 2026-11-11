# FreeNet-X Fleet Node — ESP-IDF build (with NAPT internet sharing)

This build uses **ESP-IDF** instead of the Arduino core so that lwIP NAPT is
available. It provides AP + STA operation where the STA uplink is shared to AP
clients via NAPT. It also keeps fleet telemetry: 30s heartbeat, config fetch,
event reporting, status LED, auto-reconnect.

## Why ESP-IDF (not Arduino core)
The Arduino ESP32 core does not expose NAPT/routing. NAPT requires the ESP-IDF
lwIP stack with `CONFIG_LWIP_NAPT` enabled. The board's USB is USB-to-UART only
(power/programming/serial); there is no USB Ethernet gadget (RNDIS/ECM), so
internet sharing is Wi-Fi only.

## Prerequisites
- ESP-IDF v5.x installed (`install.sh esp32`), exported (`export.sh`).
- Python 3.8+.

## Build & flash
```sh
cd firmware/esp-idf
idf.py set-target esp32
idf.py menuconfig   # verify Component config -> LWIP -> Enable NAPT is [*]
idf.py build
idf.py -p COMx flash monitor   # Windows: COM3 etc.
```

In `menuconfig`:
- FreeNet-X Node Configuration → SSID/password, node ID, token, API base
- Component config → LWIP → `[*] Enable IP forwarding`
- Component config → LWIP → `[*] Enable NAT` (requires IP forwarding)
- (DNS is always enabled in this lwIP port.)

Kconfig symbols: `CONFIG_LWIP_IP_FORWARD=y`, `CONFIG_LWIP_IPV4_NAPT=y`
(note: `CONFIG_LWIP_NAPT` does **not** exist in v5.3.x).

`sdkconfig.defaults` already sets both; verify after menuconfig. It also sets
`CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y` because the DOIT DEVKIT V1 has 4 MB flash
(otherwise esptool warns that the image header says 2 MB).

## Network layout
- AP IP (static): `192.168.4.1` (DHCP pool served to AP clients)
- AP channel: 6, `max_connection` = 4
- STA uplink: authorized SSID (open / WPA / WPA2 / WPA3);
  auto-reconnect via `WIFI_EVENT_STA_DISCONNECTED`
- Only the configured SSID is joined. The node never scans for or auto-joins
  arbitrary open networks and never captures credentials.
- NAPT is enabled automatically on `IP_EVENT_STA_GOT_IP` via
  `esp_netif_napt_enable(ap_netif)` (public lwIP API; disables NAPT on other
  interfaces)

## Configure per board
No source edits needed. Run `idf.py menuconfig` and open
**FreeNet-X Node Configuration**:
- Uplink Wi-Fi SSID / password (authorized network)
- AP SSID / password (private AP; empty password = open)
- Node ID / bearer token / API base URL
- Heartbeat interval (default 30000 ms)

Or append the same values to `sdkconfig.defaults` / `sdkconfig`:
```sh
CONFIG_FREENETX_UPLINK_SSID="MyWiFi"
CONFIG_FREENETX_UPLINK_PASSWORD="secret"
CONFIG_FREENETX_NODE_ID="node-001"
CONFIG_FREENETX_NODE_TOKEN="secret-token"
CONFIG_FREENETX_API_BASE="http://192.168.1.10:8000"
```

Each board must use a unique node ID + token also provisioned in
`FREENETX_NODE_TOKENS` on the server. On boot the firmware warns when the
uplink SSID or node token is still a placeholder.

## Status LED
The DOIT ESP32 DEVKIT V1 on-board LED is on **GPIO2** and is **blue**.
The firmware lights it as soon as power is applied (USB plug-in), with no
button press or serial command:
- **Solid blue** — node is working: AP is up and, if an uplink is configured,
  the uplink is connected.
- **Slow blink** (short on-pulse every ~0.85 s) — a configured uplink is
  disconnected/reconnecting.
- Set `FreeNet-X Node Configuration → Status LED GPIO` to `-1` to disable it,
  or change the GPIO / active level for an external LED.

## Analog sensor + binary quantization
`main/quantize_utils.h` implements Min-Max quantization of a float to one
signed byte and back:

```
int8  = round((value - min) / (max - min) * 255) - 128
value ≈ (int8 + 128) / 255 * (max - min) + min
```

The firmware samples an analog sensor with the `esp_adc` oneshot driver and
logs a live comparison every 5 s:
```
[SENSOR] raw=167mV q=-115 restored=168.2mV err=1.2mV packet=6B
```
`compressed_sensor_t` is 6 bytes (node id + quantized value + timestamp)
instead of 9+ bytes with a float. The quantized value is also sent in the
heartbeat as `adc_q` (with `adc_mv`).

`menuconfig → FreeNet-X Node Configuration`:
- `Analog sensor GPIO` — ADC1 pin, default **GPIO34** (input-only, safe),
  valid 32–39, or `-1` to disable
- `Analog sensor range minimum/maximum (mV)` — set to the sensor's real
  output span (default 0–3300 mV) to use the full 8-bit resolution

> Feed only 0–3.3 V into the ADC pin. GPIO34–39 are input-only and are the
> safest choice. Reading a floating pin gives noise; connect the sensor
> output (or a potentiometer for testing) to the configured pin.

Server-side reconstruction (Python):
```python
def dequantize_i8(q, lo, hi):
    return ((q + 128) / 255.0) * (hi - lo) + lo
```

## Dynamic CPU clock scaling (DFS)
The firmware uses the ESP-IDF power-management core (`esp_pm`) instead of
Arduino's `setCpuFrequencyMhz()`, because the Wi-Fi stack, RTOS tick and APB
clock must stay consistent when the CPU clock changes.

Workload is measured from FreeRTOS per-task run-time counters and the CPU is
moved between three tiers about every 2 s:

| Measured load | Tier | Default |
|---|---|---|
| below 30 % | idle | **80 MHz** |
| 30–70 % | medium | **160 MHz** |
| above 70 % | high | **240 MHz** |

HTTP work (internet check, heartbeat, config fetch, event report) takes a
short high-tier boost, so requests never start at 80 MHz. Light sleep stays
disabled — the AP and NAPT must keep running.

Serial log:
```
[CPU] DFS enabled: 80/160/240 MHz tiers, light sleep off
[CPU] workload=4% -> 80 MHz (tiers 80/160/240)
[CPU] workload=52% -> 160 MHz (tiers 80/160/240)
[CPU] workload=88% -> 240 MHz (tiers 80/160/240)
```
The heartbeat also reports `cpu_mhz` and `cpu_load_pct`.

`menuconfig → FreeNet-X Node Configuration`:
- `Enable dynamic CPU clock scaling (DFS)` — on by default
- `CPU frequency - idle/medium/high tier (MHz)` — defaults 80/160/240
- `CPU load sampling interval (ms)` — default 2000
- `Run a one-shot CPU tier self-test at boot` — off by default; enable it on a
  new board to log the requested vs. actual frequency for all three tiers

## Behaviour
- Starts automatically on USB power: AP is live within ~1 s of boot.
- Auto-connects to the authorized uplink on boot.
- Auto-reconnects on disconnect with a 2 s backoff timer (so a missing
  uplink does not spin the event loop).
- Auto AP start on boot; AP at `192.168.4.1`.
- Sets the STA interface as the default netif so the node's own
  heartbeat/config HTTP traffic goes out through the uplink.
- Shares uplink to AP clients via NAPT (≈4-8 clients, ≈10-20 Mbps).
- Propagates the uplink's DNS server to AP DHCP clients (clients can resolve
  names, not just reach IPs).
- Sends heartbeat every 30s (own FreeRTOS task) to `API_BASE`;
  LED on = last heartbeat accepted.
- Fetches node config each sync; reports RSSI sample every 5 syncs.
- Logs AP client connect/disconnect with MAC addresses and the IP assigned
  by the DHCP server.

## Limitations
- NAPT supports ~4-8 clients, low throughput, no IPv6, no QoS/bandwidth limit.
- NAPT requires validation on your hardware; throughput depends on RSSI.

## Arduino build
The Arduino sketch (`../FreeNetX_Fleet_Node.ino`) remains available for
telemetry-only nodes. Its experimental NAPT path is disabled by default and is
not supported; use this ESP-IDF build for internet sharing.
