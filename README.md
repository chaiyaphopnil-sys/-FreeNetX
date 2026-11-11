# FreeNet-X Autonomous Network Intelligence V2.0 — MVP

## Selected specification
- Distributed mode: Fleet
- Synchronization: Continuous (30-second heartbeat in firmware)
- Data sources: Both owned and authorized sources
- Controller: DOIT ESP32 DEVKIT V1
- Server: Python FastAPI
- Storage: SQLite
- Identity: unique node ID + per-node bearer token
- Self-propagation: disabled; no discovery-based installation or copying

## Architecture
Each ESP32 node connects to an authorized upstream Wi-Fi network and can
broadcast a separate private Wi-Fi AP. It sends periodic health telemetry to a
central FastAPI server. The server validates that the node ID is allowlisted
and that its token matches the provisioned token, then stores the node status
and provenance-labelled events in SQLite.

This MVP is a fleet telemetry/control-plane foundation. It does not yet
implement routed internet sharing/NAPT, multi-AP bandwidth bonding, AI model
training, OTA deployment, or USB Ethernet.

## Repository
- `server/app.py`: FastAPI service, node allowlist, heartbeat and event ingestion.
- `firmware/FreeNetX_Fleet_Node.ino`: ESP32 fleet node sketch.
- `tests/test_api.py`: API tests.
- `requirements.txt`: Python dependencies.

## Run the server (lab LAN)
1. Create a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`
3. Set unique credentials before starting:
   - Linux/macOS: `export FREENETX_NODE_TOKENS='node-001:LONG_RANDOM_SECRET,node-002:ANOTHER_SECRET'`
   - PowerShell: `$env:FREENETX_NODE_TOKENS='node-001:LONG_RANDOM_SECRET,node-002:ANOTHER_SECRET'`
   - Optional admin token: `$env:FREENETX_ADMIN_TOKEN='LONG_ADMIN_SECRET'`
   - Optional MQTT bridge: `$env:FREENETX_MQTT_BROKER='192.168.1.10'`
4. Start: `uvicorn server.app:app --host 0.0.0.0 --port 8000`
5. Health check: `http://SERVER_LAN_IP:8000/health`

Do not expose this HTTP lab service to the public internet. For deployment,
terminate HTTPS at a properly configured reverse proxy, validate certificates
on the ESP32, use unique high-entropy tokens per node, firewall the service,
and rotate credentials. Never use the development token in a deployment.

## Configure and flash ESP32 (Arduino telemetry build)
1. Install Arduino IDE and the ESP32 board package.
2. Install ArduinoJson library.
3. Edit `UPLINK_SSID`, `UPLINK_PASS`, `NODE_ID`, `NODE_TOKEN`, and `API_BASE`.
4. Give each board a unique node ID and unique token that is also provisioned
   in `FREENETX_NODE_TOKENS`.
5. Upload `firmware/FreeNetX_Fleet_Node.ino` to DOIT ESP32 DEVKIT V1.
6. Open Serial Monitor at 115200 baud.
7. Confirm AP startup, upstream association, and successful HTTP heartbeat.

The common DEVKIT V1 USB connector is a USB-to-UART bridge: USB is for power,
programming and serial logs, not a USB Ethernet gadget. Internet sharing on this
board is Wi-Fi only (NAPT) and requires the ESP-IDF build below.

## Configure and flash ESP32 (ESP-IDF build with NAPT internet sharing)
For routed internet sharing (STA uplink -> private AP via NAPT), use the
ESP-IDF project in `firmware/esp-idf/`. Arduino core does not support NAPT.
See `firmware/esp-idf/README.md` for build/flash steps and menuconfig
(`CONFIG_LWIP_IP_FORWARD=y`, `CONFIG_LWIP_IPV4_NAPT=y`). Credentials and the
API base URL are set under **FreeNet-X Node Configuration** in menuconfig, not
by editing source. The build targets 4 MB flash and propagates the uplink DNS
server to AP clients.

## Tests
Run: `pytest -q`
Tests cover health, token enforcement, allowlisted heartbeat, source-type
validation, and rejection of unknown nodes.

## Wiring and BOM
No external wiring required for the telemetry MVP:
- DOIT ESP32 DEVKIT V1 x 1 per node
- USB data cable x 1 per node
- regulated 5 V USB supply x 1 per node
- Wi-Fi uplink authorized by its owner
- server computer on a trusted LAN

## Test plan
1. API health returns `{"status":"ok"}`.
2. Heartbeat without token is rejected (401).
3. Allowlisted node with correct token is accepted.
4. Unknown node is rejected (401).
5. Owned and authorized event provenance is accepted.
6. Unrecognized source type is rejected (422).
7. Firmware reports a heartbeat every 30 seconds while uplink is available.
8. Remove and restore the authorized uplink; confirm reconnection and heartbeat resumes.
9. Register a second board with a different ID/token; confirm both appear in fleet list.
10. Run for 60 minutes and record resets, missed heartbeats, RSSI and server errors.

## Capability truth table
### Implemented in this code package (V2.0)
- ESP32 AP + STA startup
- periodic heartbeat to central server (30s)
- unique node identity field
- per-node token authentication at API
- fleet status persistence in SQLite
- owned/authorized provenance validation for event ingestion
- node status history + multi-node fleet view
- central configuration manager (per-node config fetch/push)
- audit log for all node actions
- basic analytics (online/offline, RSSI averages)
- optional MQTT bridge for heartbeat/event ingestion
- status LED (blue = uplink healthy)
- binary stream quantization (float32 → int8) with live ADC sampling
- dynamic CPU clock scaling (80/160/240 MHz) based on measured workload
- gzip response compression on server
- rule-based anomaly detection endpoint
- automated API tests (14 passing)

### Hardware/platform supported but not fully validated here
- stable operation with multiple physical ESP32 boards
- Wi-Fi coverage, throughput and reconnect behaviour
- NAPT internet sharing via ESP-IDF build (`firmware/esp-idf/`); requires
  `CONFIG_LWIP_IP_FORWARD` + `CONFIG_LWIP_IPV4_NAPT` and on-hardware validation

### Not implemented (still out of scope for V2.0)
- automatic firmware distribution / OTA (signed)
- AI/ML model training or autonomous code modification (only basic analytics)
- mesh routing or multi-AP bandwidth aggregation
- USB Ethernet gadget on the original DEVKIT V1 (use ESP32-S3/Raspberry Pi/GL.iNet)
- one-million-scenario simulation or benchmark claims

## Security and non-propagation guarantees
- Nodes do not copy firmware to nearby devices.
- The server accepts telemetry only from provisioned node IDs with matching tokens.
- No endpoint executes shell commands or arbitrary code on nodes.
- No credential harvesting, captive-portal bypass, MAC impersonation,
  deauthentication, beacon flooding, quota circumvention or unauthorized scanning.
- OTA is intentionally excluded until signed firmware, rollback protection,
  staged rollout, and explicit fleet-admin authorization are implemented.

## Next engineering gates
1. Validate this API locally with pytest.
2. Compile and flash the sketch on one ESP32.
3. Verify heartbeat from one node.
4. Add a second authorized node.
5. Validate long-run operation.
6. Only then add signed OTA and NAPT in separate reviewed milestones.
