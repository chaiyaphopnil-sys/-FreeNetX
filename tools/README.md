# FreeNet-X PC Monitor

หน้าจอแสดงผลภาษาไทยสำหรับโหนด FreeNet-X อ่านข้อมูลสดจากบอร์ดผ่าน USB serial
จึงใช้งานได้**โดยไม่ต้องมี uplink หรือเซิร์ฟเวอร์** และสลับไปดึงข้อมูลจาก
FastAPI (`/v1/fleet`) ได้

## Run

Double-click `pc_monitor.bat`, or the desktop shortcut
`FreeNet-X-PC-Monitor.bat`. Defaults: **COM3, 115200**.

From a terminal:

```sh
python pc_monitor.py                        # GUI, serial COM3
python pc_monitor.py --port COM5            # another port
python pc_monitor.py --demo                 # GUI preview with fake values
python pc_monitor.py --headless 15          # text mode, 15 seconds
python pc_monitor.py --server http://192.168.1.10:8000
```

Requires Python 3.10+ and `pyserial` (`pip install pyserial`). Tkinter ships
with the standard Windows Python installer. No other packages are needed.

## What it shows (serial mode)

แท็บ **ภาพรวม** — การ์ดสถานะ 6 ใบ + แถบรายละเอียด:

| Card | Meaning |
|---|---|
| ความเร็ว CPU | ค่าล่าสุดจาก `[CPU] ... -> N MHz` (80 / 160 / 240) |
| ภาระ CPU | เปอร์เซ็นต์ภาระงาน พร้อมสีตามขั้น 30 % / 70 % |
| Access Point | SSID, IP ของ AP, จำนวนลูกข่าย, GPIO ของไฟสถานะ |
| เซนเซอร์ ADC | ค่าดิบ millivolt และค่า `q` |
| การบีบอัด int8 | ขนาดแพ็กเก็ต, ค่าที่แปลงกลับ, ความคลาดเคลื่อน |
| Uplink / Heartbeat | ชื่อ uplink, IP, สถานะ HTTP ของ heartbeat |
| ไฟ LED / การเชื่อมต่อ | คำอธิบายโหมดไฟ + NAPT / เน็ต / config / event |

แท็บ **กราฟ** — 4 กราฟตามการทำงานจริงของเฟิร์มแวร์:

| Graph | Meaning |
|---|---|
| ความเร็ว CPU (MHz) | ขั้นบันได 80 → 160 → 240 ตาม DFS |
| ภาระ CPU (%) | ภาระงานที่วัดทุก 2 วินาที + เส้นเตือน 30/70 % |
| เซนเซอร์ ADC (mV) | ค่าจริงเทียบค่าที่แปลงกลับหลังบีบอัด int8 |
| สถานะลิงก์ | เน็ตออก / uplink ต่อได้แต่เน็ตไม่ออก / uplink หลุด |

แท็บ **บันทึก** — raw log พร้อมตัวกรองระดับ info/warn/error, หยุดชั่วคราว,
บันทึกไฟล์, ล้าง
แท็บ **เซิร์ฟเวอร์** — ตารางโหนดจาก `/v1/fleet` (ออนไลน์, RSSI, IP, เฟิร์มแวร์,
เห็นล่าสุด)

## Server mode

Polls `GET /v1/fleet` every 3 s and lists every known node with online state,
RSSI, IP, firmware and last-seen age. `/v1/fleet` is unauthenticated in the
current server; heartbeat ingestion still requires the per-node token.

## Auto-start when the USB cable is plugged in

`auto_monitor.py` watches Windows for an ESP32 USB serial port (CP210x,
CH340, FTDI or native ESP32 USB) and opens the dashboard automatically. When
the board is unplugged it closes the window it opened.

Double-click `auto_monitor.bat`, or from a terminal:

```sh
python auto_monitor.py               # run the watcher (visible console)
python auto_monitor.py --once        # list detected ESP32 ports
python auto_monitor.py --port COM3   # only watch one port
python auto_monitor.py --install     # start at Windows login (recommended)
python auto_monitor.py --uninstall   # remove the login entry
```

`--install` creates `FreeNetX Auto Monitor.lnk` in the Windows Startup folder.
After that, logging in starts the watcher invisibly; plugging the board in
opens the dashboard, unplugging closes it. Only one watcher can run at a time
(it uses a localhost singleton lock), and it will not open a second window for
a port that already has a monitor attached.

Log file: `%TEMP%\freenetx-auto-monitor.log`

## Graph Dashboard (Beautiful Themed Graphs)

โปรแกรมแสดงผลกราฟแบบ Real-time ด้วยธีมมืดทันสมัย สำหรับดูค่า CPU, ADC, Siphon Queue, สถานะลิงก์

Double-click `graph_dashboard.bat`, or the desktop shortcut `FreeNet-X-Graph-Dashboard.bat`.

From a terminal:
```sh
python graph_dashboard.py                  # GUI, serial COM3
python graph_dashboard.py --demo           # ข้อมูลจำลอง
python graph_dashboard.py --port COM5      # พอร์ตอื่น
```

Features:
- **Beautiful animated graphs** with glow effect, smooth lines, gradient background
- **Real-time status bar** showing CPU, Load, ADC, Link, Siphon, Uptime
- **4 graphs**: CPU MHz (DFS), CPU Load (%), Sensor ADC (mV), Siphon Queue (งาน)
- **Thai language** throughout
- **Dark theme** with cyan/purple/green/amber accents
- **Auto-connect** to serial port on startup

## Notes

- Opening the serial port can reset the board once (DTR/RTS), after which the
  app captures the whole boot sequence.
- Serial mode is read-only: the PC never sends commands to the node.
- Close the app before running `idf.py flash` / `idf.py monitor`, because a COM
  port can only be open in one program at a time.
