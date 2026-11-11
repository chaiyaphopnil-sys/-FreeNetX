#!/usr/bin/env python3
"""FreeNet-X PC Monitor (Thai UI).

จอแสดงผลสำหรับบอร์ด FreeNet-X อ่านข้อมูลสดจาก USB serial (COMx) หรือดึง
รายชื่อโหนดจากเซิร์ฟเวอร์ FastAPI (/v1/fleet)

    python pc_monitor.py                  # GUI, COM3
    python pc_monitor.py --port COM5
    python pc_monitor.py --demo
    python pc_monitor.py --headless 15
    python pc_monitor.py --server http://192.168.1.10:8000
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from tkinter import filedialog, font as tkfont, messagebox, ttk
import tkinter as tk

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # แจ้งเตือนใน GUI แทนการ crash
    serial = None
    list_ports = None

# --------------------------------------------------------------------------
# การอ่าน log ของบอร์ด
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CPU_RE = re.compile(r"\[CPU\] workload=(\d+)% -> (\d+) MHz")
SENSOR_RE = re.compile(
    r"\[SENSOR\] raw=(-?\d+)mV q=(-?\d+) restored=([\d.]+)mV "
    r"err=([\d.]+)mV packet=(\d+)B"
)
AP_RE = re.compile(r"AP\+STA started; AP SSID=(\S+)")
DHCP_RE = re.compile(r"DHCP server started on interface \S+ with IP: ([\d.]+)")
LED_RE = re.compile(r"Status LED on GPIO(-?\d+)")
NET_RE = re.compile(r"\[NET\] internet check HTTP=(\d+) \((\w+)\)")
SYNC_RE = re.compile(r"\[SYNC\] heartbeat HTTP=(\d+)")
CFG_RE = re.compile(r"\[CONFIG\] HTTP=(\d+)")
EVENT_RE = re.compile(r"\[EVENT\] HTTP=(\d+)")
BOOT_RE = re.compile(r"^(rst:0x|boot: ESP-IDF|ESP-ROM:)")
NAPT_FAIL_RE = re.compile(r"NAPT enable failed")
DNS_WARN_RE = re.compile(r"Uplink has no usable IPv4 DNS")
DNS_INFO_RE = re.compile(r"AP DHCP DNS -> ([\\d.]+)")
SENSOR_READY_RE = re.compile(r"\\[SENSOR\\] ADC1 GPIO(\\d+) ready")
SENSOR_DISABLED_RE = re.compile(r"\\[SENSOR\\] (ADC sampling disabled|GPIO\\d+ is not an ADC1 pin)")
DFS_ENABLED_RE = re.compile(r"\\[CPU\\] DFS enabled: (\\d+)/(\\d+)/(\\d+) MHz")
PLACEHOLDER_UPLINK_RE = re.compile(r"UPLINK_SSID is still a placeholder")
PLACEHOLDER_TOKEN_RE = re.compile(r"NODE_TOKEN is still a placeholder")
SIPHON_DROP_RE = re.compile(r"\\[Siphon\\] Queue full, dropping task")
SIPHON_INIT_RE = re.compile(r"Siphon.*Queue initialized.*depth=(\d+)")
RSSI_RE = re.compile(r"STA RSSI=(-?\d+) dBm, channel=(\d+), authmode=(\d+)")
MEM_RE = re.compile(r"\[MEM\] free heap=(\d+) bytes, min=(\d+)")
BOOT_REASON_RE = re.compile(r"^(rst:0x\w+) \(([^)]+)\)")
UPLINK_RE = re.compile(r"AP=(\S+) uplink=(.+)$")
STA_IP_RE = re.compile(r"STA got IP ([\d.]+)")
STA_DISC_RE = re.compile(r"STA disconnected")
RECONNECT_RE = re.compile(r"Reconnecting to uplink")
NAPT_RE = re.compile(r"NAPT enabled")
CLIENT_CONN_RE = re.compile(r"Client connected: ([0-9A-Fa-f:]{17})")
CLIENT_DISC_RE = re.compile(r"Client disconnected: ([0-9A-Fa-f:]{17})")
SIPHON_RE = re.compile(r"SEND_TO_PC:(\d+):(\S+)")

MAX_POINTS = 180


@dataclass
class NodeState:
    port: str = ""
    ap_ssid: str = "-"
    ap_ip: str = "-"
    led_gpio: str = "-"
    cpu_mhz: int = 0
    cpu_load: int = 0
    adc_mv: int | None = None
    adc_q: int | None = None
    adc_restored: float | None = None
    adc_err: float | None = None
    packet_bytes: int | None = None
    internet: str = "unknown"
    heartbeat_http: int | None = None
    config_http: int | None = None
    event_http: int | None = None
    uplink_name: str = "(รอข้อมูล)"
    uplink_configured: bool | None = None
    sta_connected: bool = False
    uplink_ip: str = "-"
    napt: bool = False
    clients: dict[str, str] = field(default_factory=dict)
    siphon_dispatched: int = 0
    # Comprehensive status fields
    napt_error: bool = False
    dns_warning: bool = False
    dns_server: str = "-"
    sensor_ready: bool = False
    sensor_gpio: int = -1
    sensor_disabled: bool = False
    dfs_enabled: bool = False
    dfs_tiers: str = "-"
    placeholder_uplink: bool = False
    placeholder_token: bool = False
    siphon_dropped: int = 0
    siphon_depth: int = 50
    wifi_disconnect_reason: int = 0
    heartbeat_count: int = 0
    # New metrics
    rssi: int = 0
    wifi_channel: int = 0
    authmode: int = 0
    free_heap: int = 0
    min_heap: int = 0
    boot_reason: str = "-"
    config_count: int = 0
    event_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    last_error: str = "-"
    boot_reason: str = "-"
    free_heap: int = 0
    boot_at: float = field(default_factory=time.monotonic)
    cpu_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    sensor_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    link_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    siphon_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))

    @property
    def link_level(self) -> int:
        """100 = เน็ตออก, 60 = ต่อ uplink ได้แต่ยังไม่ออกเน็ต, 20 = uplink หลุด"""
        if self.internet == "online":
            return 100
        if self.sta_connected:
            return 60
        return 20

    @property
    def uptime_s(self) -> int:
        return max(0, int(time.monotonic() - self.boot_at))

    @property
    def led_mode(self) -> tuple[str, str]:
        if self.uplink_configured is False:
            return "ติดค้าง", "โหมด AP อย่างเดียว"
        if self.uplink_configured is None:
            return "รอข้อมูล", "ยังไม่ได้รับสถานะ"
        if not self.sta_connected:
            return "กะพริบช้า", "uplink หลุด กำลังเชื่อมต่อใหม่"
        if self.internet == "offline":
            return "กะพริบ 2 ครั้ง", "ต่อ uplink ได้แต่เน็ตไม่ออก"
        return "ติดค้าง", "ทำงานปกติ"


def clean_line(raw: str) -> str:
    return ANSI_RE.sub("", raw).replace("\r", "").strip()


def parse_line(line: str, st: NodeState) -> bool:
    """อัปเดตสถานะจาก log หนึ่งบรรทัด คืนค่า True เมื่อข้อมูลเปลี่ยน"""
    changed = False
    now = time.monotonic()

    if BOOT_RE.match(line):
        st.boot_at = now
        st.heartbeat_http = None
        st.config_http = None
        st.event_http = None
        st.internet = "unknown"
        st.sta_connected = False
        st.uplink_ip = "-"
        st.napt = False
        st.clients.clear()
        st.cpu_points.clear()
        st.sensor_points.clear()
        st.link_points.clear()
        st.siphon_points.clear()
        st.napt_error = False
        st.dns_warning = False
        st.dns_server = "-"
        st.sensor_ready = False
        st.sensor_gpio = -1
        st.sensor_disabled = False
        st.dfs_enabled = False
        st.dfs_tiers = "-"
        st.placeholder_uplink = False
        st.placeholder_token = False
        st.siphon_dropped = 0
        st.siphon_depth = 50
        st.wifi_disconnect_reason = 0
        st.heartbeat_count = 0
        st.config_count = 0
        st.event_count = 0
        st.error_count = 0
        st.warning_count = 0
        st.last_error = "-"
        st.free_heap = 0
        st.link_points.append((now, st.link_level))
        changed = True

    m = CPU_RE.search(line)
    if m:
        st.cpu_load = int(m.group(1))
        st.cpu_mhz = int(m.group(2))
        st.cpu_points.append((now, st.cpu_mhz, st.cpu_load))
        changed = True

    m = SENSOR_RE.search(line)
    if m:
        st.adc_mv = int(m.group(1))
        st.adc_q = int(m.group(2))
        st.adc_restored = float(m.group(3))
        st.adc_err = float(m.group(4))
        st.packet_bytes = int(m.group(5))
        st.sensor_points.append((now, st.adc_mv, st.adc_restored))
        changed = True

    m = AP_RE.search(line)
    if m:
        st.ap_ssid = m.group(1)
        changed = True

    m = DHCP_RE.search(line)
    if m:
        st.ap_ip = m.group(1)
        changed = True

    m = LED_RE.search(line)
    if m:
        st.led_gpio = m.group(1)
        changed = True

    m = NET_RE.search(line)
    if m:
        st.internet = m.group(2)
        st.link_points.append((now, st.link_level))
        changed = True

    m = SYNC_RE.search(line)
    if m:
        st.heartbeat_http = int(m.group(1))
        changed = True

    m = CFG_RE.search(line)
    if m:
        st.config_http = int(m.group(1))
        changed = True

    m = EVENT_RE.search(line)
    if m:
        st.event_http = int(m.group(1))
        changed = True

    m = UPLINK_RE.search(line)
    if m:
        st.uplink_name = m.group(2).strip()
        st.uplink_configured = "not configured" not in st.uplink_name
        changed = True

    m = STA_IP_RE.search(line)
    if m:
        st.sta_connected = True
        st.uplink_ip = m.group(1)
        st.link_points.append((now, st.link_level))
        changed = True

    if STA_DISC_RE.search(line) or RECONNECT_RE.search(line):
        st.sta_connected = False
        st.link_points.append((now, st.link_level))
        changed = True

    if NAPT_RE.search(line):
        st.napt = True
        changed = True

    m = CLIENT_CONN_RE.search(line)
    if m:
        st.clients.setdefault(m.group(1), "-")
        changed = True

    m = CLIENT_DISC_RE.search(line)
    if m:
        st.clients.pop(m.group(1), None)
        changed = True

    m = CLIENT_IP_RE.search(line)
    if m:
        st.clients[m.group(2)] = m.group(1)
        changed = True

    m = SIPHON_RE.search(line)
    if m:
        st.siphon_dispatched += 1
        st.siphon_points.append((now, st.siphon_dispatched))
        changed = True

    m = NAPT_FAIL_RE.search(line)
    if m:
        st.napt_error = True
        changed = True

    m = DNS_WARN_RE.search(line)
    if m:
        st.dns_warning = True
        changed = True

    m = DNS_INFO_RE.search(line)
    if m:
        st.dns_server = m.group(1)
        changed = True

    m = SENSOR_READY_RE.search(line)
    if m:
        st.sensor_ready = True
        st.sensor_gpio = int(m.group(1))
        st.sensor_disabled = False
        changed = True

    m = SENSOR_DISABLED_RE.search(line)
    if m:
        st.sensor_disabled = True
        st.sensor_ready = False
        changed = True

    m = DFS_ENABLED_RE.search(line)
    if m:
        st.dfs_enabled = True
        st.dfs_tiers = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
        changed = True

    m = PLACEHOLDER_UPLINK_RE.search(line)
    if m:
        st.placeholder_uplink = True
        changed = True

    m = PLACEHOLDER_TOKEN_RE.search(line)
    if m:
        st.placeholder_token = True
        changed = True

    m = SIPHON_DROP_RE.search(line)
    if m:
        st.siphon_dropped += 1
        changed = True

    m = SIPHON_INIT_RE.search(line)
    if m:
        st.siphon_depth = int(m.group(1))
        changed = True

    m = WIFI_REASON_RE.search(line)
    if m:
        st.wifi_disconnect_reason = int(m.group(1))
        changed = True

    m = RSSI_RE.search(line)
    if m:
        st.rssi = int(m.group(1))
        st.wifi_channel = int(m.group(2))
        st.authmode = int(m.group(3))
        changed = True

    m = MEM_RE.search(line)
    if m:
        st.free_heap = int(m.group(1))
        st.min_heap = int(m.group(2))
        changed = True

    m = BOOT_REASON_RE.search(line)
    if m:
        st.boot_reason = f"{m.group(1)} ({m.group(2)})"
        changed = True

    # Count HTTP responses
    m = SYNC_RE.search(line)
    if m:
        st.heartbeat_count += 1
        changed = True

    m = CFG_RE.search(line)
    if m:
        st.config_count += 1
        changed = True

    m = EVENT_RE.search(line)
    if m:
        st.event_count += 1
        changed = True

    # Count errors and warnings
    if "E (" in line or "ERROR" in line.upper():
        st.error_count += 1
        st.last_error = line[:80]
        changed = True
    elif "W (" in line or "WARN" in line.upper():
        st.warning_count += 1
        changed = True

    return changed


# --------------------------------------------------------------------------
# ตัวอ่านพอร์ต / ตัวดึงข้อมูลเซิร์ฟเวอร์
# --------------------------------------------------------------------------
def monitor_lock_path(port: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", port)
    return os.path.join(tempfile.gettempdir(), f"freenetx-monitor-{safe}.lock")


def write_monitor_lock(port: str) -> None:
    try:
        with open(monitor_lock_path(port), "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def clear_monitor_lock(port: str) -> None:
    try:
        os.remove(monitor_lock_path(port))
    except OSError:
        pass


class SerialReader(threading.Thread):
    def __init__(self, port: str, baud: int, out: queue.Queue, stop: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.out = out
        self.stop = stop

    def run(self) -> None:
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.2)
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self.out.put(("status", f"เปิดพอร์ตไม่สำเร็จ: {exc}"))
            return

        self.out.put(("status", f"เชื่อมต่อแล้ว {self.port} @ {self.baud}"))
        try:
            while not self.stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                line = clean_line(raw.decode("utf-8", errors="replace"))
                if line:
                    self.out.put(("line", line))
        except Exception as exc:  # noqa: BLE001
            self.out.put(("status", f"อ่านข้อมูลผิดพลาด: {exc}"))
        finally:
            try:
                ser.close()
            except Exception:
                pass
            self.out.put(("status", "ตัดการเชื่อมต่อแล้ว"))


class FleetPoller(threading.Thread):
    def __init__(self, base: str, out: queue.Queue, stop: threading.Event):
        super().__init__(daemon=True)
        self.base = base.rstrip("/")
        self.out = out
        self.stop = stop

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.base + "/v1/fleet", timeout=4) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.out.put(("fleet", data))
            except Exception as exc:  # noqa: BLE001
                self.out.put(("fleet_error", str(exc)))
            self.stop.wait(3.0)


# --------------------------------------------------------------------------
# ธีมและสี
# --------------------------------------------------------------------------
BG = "#070c18"
SURFACE = "#0c1526"
CARD = "#111d33"
CARD2 = "#182742"
BORDER = "#22314f"
FG = "#e8eefc"
MUTED = "#8fa3c4"
DIM = "#5d7093"
ACCENT = "#22d3ee"
PURPLE = "#a78bfa"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#f87171"
THAI_FONT = "Leelawadee UI"


def fnt(size: int = 10, weight: str = "normal") -> tuple:
    return (THAI_FONT, size, weight)


def pick_font(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except Exception:
        return "TkDefaultFont"
    for name in ("Leelawadee UI", "Leelawadee", "Sarabun", "Noto Sans Thai",
                 "Segoe UI", "Tahoma"):
        if name in families:
            return name
    return "TkDefaultFont"


class StatCard(tk.Frame):
    """การ์ดแสดงค่าหลัก 1 ค่า พร้อมแถบสีด้านซ้าย"""

    def __init__(self, parent: tk.Misc, title: str, color: str = ACCENT):
        super().__init__(parent, bg=BORDER, bd=0, highlightthickness=0)
        inner = tk.Frame(self, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(inner, bg=color, width=4).pack(side="left", fill="y")
        body = tk.Frame(inner, bg=CARD)
        body.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        self.title = tk.Label(body, text=title, bg=CARD, fg=MUTED, font=fnt(9, "bold"))
        self.title.pack(anchor="w")
        self.value = tk.Label(body, text="-", bg=CARD, fg=FG, font=fnt(22, "bold"))
        self.value.pack(anchor="w", pady=(2, 0))
        self.sub = tk.Label(body, text="", bg=CARD, fg=DIM, font=fnt(9),
                            justify="left", anchor="w")
        self.sub.pack(anchor="w", fill="x")
        self.accent = color

    def set(self, value: str, sub: str = "", color: str | None = None) -> None:
        self.value.configure(text=value, fg=color or FG)
        self.sub.configure(text=sub)


class Badge(tk.Frame):
    def __init__(self, parent: tk.Misc, label: str):
        super().__init__(parent, bg=CARD2, highlightbackground=BORDER,
                         highlightthickness=1)
        self.dot = tk.Canvas(self, width=12, height=12, bg=CARD2,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 4), pady=8)
        self.dot_id = self.dot.create_oval(2, 2, 10, 10, fill=DIM, outline="")
        self.label = tk.Label(self, text=label, bg=CARD2, fg=MUTED, font=fnt(9))
        self.label.pack(side="left", padx=(0, 12))

    def set(self, text: str, color: str) -> None:
        self.label.configure(text=text, fg=FG)
        self.dot.itemconfigure(self.dot_id, fill=color)


class Chart(tk.Canvas):
    """กราฟเส้น/ขั้นบันได ใช้เวลาจริงเป็นแกน X"""

    def __init__(self, parent: tk.Misc, title: str, unit: str = "",
                 y_min: float | None = 0, y_max: float | None = None,
                 thresholds: tuple[float, ...] = (),
                 y_grid: tuple[float, ...] = (),
                 y_labels: dict[float, str] | None = None):
        super().__init__(parent, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        self.title = title
        self.unit = unit
        self.y_min = y_min
        self.y_max = y_max
        self.thresholds = thresholds
        self.y_grid = y_grid
        self.y_labels = y_labels or {}
        self.series: list[dict] = []
        self.empty_text = "กำลังรอข้อมูลจากบอร์ด..."
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_data(self, series: list[dict]) -> None:
        self.series = series
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 120)
        h = max(self.winfo_height(), 80)
        left, right, top, bottom = 64, 16, 46, 26
        plot_w = max(w - left - right, 10)
        plot_h = max(h - top - bottom, 10)

        self.create_text(16, 16, text=self.title, anchor="w", fill=FG,
                         font=fnt(10, "bold"))
        self.create_line(16, 36, w - 16, 36, fill=BORDER)

        # legend
        lx = 16
        for s in self.series:
            self.create_line(lx, 60, lx + 14, 60, fill=s["color"], width=2)
            self.create_text(lx + 18, 60, text=s["name"], anchor="w",
                             fill=MUTED, font=fnt(8))
            lx += 30 + len(s["name"]) * 8

        points = [p for s in self.series for p in s["points"]]
        if not points:
            self.create_text(w / 2, h / 2, text=self.empty_text, fill=DIM,
                             font=fnt(10))
            return

        t_min = min(p[0] for p in points)
        t_max = max(p[0] for p in points)
        span = t_max - t_min
        if span <= 0:
            span = 1.0

        y_values = [p[1] for p in points]
        lo = self.y_min if self.y_min is not None else min(y_values)
        hi = self.y_max if self.y_max is not None else max(y_values)
        if self.y_max is None:
            pad = max((hi - lo) * 0.12, 1.0)
            lo, hi = lo - pad, hi + pad
        if hi <= lo:
            hi = lo + 1

        def x_of(t: float) -> float:
            return left + (t - t_min) / span * plot_w

        def y_of(v: float) -> float:
            clamped = min(max(v, lo), hi)
            return top + (1 - (clamped - lo) / (hi - lo)) * plot_h

        # grid + y labels
        grid_values = self.y_grid or tuple(lo + (hi - lo) * i / 4 for i in range(5))
        for value in grid_values:
            y = y_of(value)
            self.create_line(left, y, left + plot_w, y, fill="#1b2942")
            label = self.y_labels.get(value, f"{value:.0f}")
            self.create_text(left - 8, y, text=label, anchor="e",
                             fill=DIM, font=fnt(8))

        # thresholds
        for th in self.thresholds:
            if lo <= th <= hi:
                y = y_of(th)
                self.create_line(left, y, left + plot_w, y,
                                 fill=AMBER, dash=(4, 4))
                self.create_text(left + plot_w - 4, y - 8, text=f"{th:.0f}",
                                 anchor="e", fill=AMBER, font=fnt(8))

        # x labels (time relative to the newest sample)
        self.create_text(left, top + plot_h + 12, text=f"-{span:.0f} วินาที",
                         anchor="w", fill=DIM, font=fnt(8))
        self.create_text(left + plot_w, top + plot_h + 12, text="ล่าสุด",
                         anchor="e", fill=DIM, font=fnt(8))

        for s in self.series:
            pts = list(s["points"])
            if not pts:
                continue
            if s.get("style") == "step":
                coords: list[float] = []
                for i, (t, v) in enumerate(pts):
                    x, y = x_of(t), y_of(v)
                    if i:
                        coords.extend((x, coords[-1]))
                    coords.extend((x, y))
                if len(coords) >= 4:
                    self.create_line(*coords, fill=s["color"], width=2)
            else:
                coords = []
                for t, v in pts:
                    coords.extend((x_of(t), y_of(v)))
                if len(coords) >= 4:
                    self.create_line(*coords, fill=s["color"], width=2,
                                     smooth=False)
            if s.get("dots"):
                for t, v in pts[-12:]:
                    x, y = x_of(t), y_of(v)
                    self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                     fill=s["color"], outline="")


# --------------------------------------------------------------------------
# แอปพลิเคชันหลัก
# --------------------------------------------------------------------------
class MonitorApp:
    TABS = (
        ("overview", "ภาพรวม"),
        ("graphs", "กราฟ"),
        ("log", "บันทึก"),
        ("server", "เซิร์ฟเวอร์"),
        ("status", "สถานะทั้งหมด"),
    )

    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        global THAI_FONT
        self.root = root
        self.args = args
        self.state = NodeState()
        self.queue: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.reader: SerialReader | None = None
        self.poller: FleetPoller | None = None
        self.log_lines: list[tuple[str, str]] = []
        self.log_paused = False
        self._demo_n = 0
        self.active_tab = "overview"

        THAI_FONT = pick_font(root)
        root.title("FreeNet-X · ศูนย์ควบคุมโหนด")
        root.configure(bg=BG)
        root.geometry("1120x820")
        root.minsize(980, 720)
        try:
            root.tk.call("tk", "scaling", 1.15)
        except Exception:
            pass

        self._build_header()
        self._build_nav()
        self._build_connbar()
        self._build_content()
        self.show_tab("overview")

        self.source_var.trace_add("write", lambda *_: self._switch_source())
        self._switch_source()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain_queue)
        root.after(1000, self._tick)

        if args.demo:
            root.after(400, self._demo_tick)

    # -- header / nav ------------------------------------------------------
    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=SURFACE, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)

        logo = tk.Canvas(header, width=40, height=40, bg=SURFACE,
                         highlightthickness=0)
        logo.pack(side="left", padx=(18, 12), pady=12)
        logo.create_rectangle(2, 2, 38, 38, fill=ACCENT, outline="")
        logo.create_text(20, 20, text="FX", fill="#04222b",
                         font=fnt(13, "bold"))

        titles = tk.Frame(header, bg=SURFACE)
        titles.pack(side="left", pady=10)
        tk.Label(titles, text="ศูนย์ควบคุม FreeNet-X", bg=SURFACE, fg=FG,
                 font=fnt(14, "bold")).pack(anchor="w")
        tk.Label(titles, text="มอนิเตอร์บอร์ด ESP32 ผ่าน USB · ภาษาไทย",
                 bg=SURFACE, fg=MUTED, font=fnt(9)).pack(anchor="w")

        self.status_pill = tk.Frame(header, bg=CARD2, highlightbackground=BORDER,
                                    highlightthickness=1)
        self.status_pill.pack(side="right", padx=18)
        self.status_dot = tk.Canvas(self.status_pill, width=12, height=12,
                                    bg=CARD2, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(10, 4), pady=8)
        self.status_dot_id = self.status_dot.create_oval(2, 2, 10, 10,
                                                         fill=DIM, outline="")
        self.status_lbl = tk.Label(self.status_pill, text="ยังไม่เชื่อมต่อ",
                                   bg=CARD2, fg=MUTED, font=fnt(9))
        self.status_lbl.pack(side="left", padx=(0, 12))

    def _build_nav(self) -> None:
        self.nav = tk.Frame(self.root, bg=BG)
        self.nav.pack(fill="x", padx=18, pady=(10, 0))
        self.nav_buttons: dict[str, tk.Button] = {}
        for key, label in self.TABS:
            button = tk.Button(
                self.nav, text=label, command=lambda k=key: self.show_tab(k),
                bg=BG, fg=MUTED, activebackground=CARD2, activeforeground=FG,
                font=fnt(10, "bold"), bd=0, padx=18, pady=8, cursor="hand2",
                relief="flat",
            )
            button.pack(side="left", padx=(0, 4))
            self.nav_buttons[key] = button

    def _build_connbar(self) -> None:
        bar = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", padx=18, pady=(8, 0))

        tk.Label(bar, text="แหล่งข้อมูล", bg=CARD, fg=MUTED,
                 font=fnt(9, "bold")).pack(side="left", padx=(12, 6), pady=8)

        self.source_var = tk.StringVar(value="server" if self.args.server else "serial")
        self.src_buttons: dict[str, tk.Radiobutton] = {}
        for text, value in (("USB", "serial"), ("เซิร์ฟเวอร์", "server")):
            rb = tk.Radiobutton(
                bar, text=text, value=value, variable=self.source_var,
                indicatoron=False, bg=CARD2, fg=MUTED, selectcolor=ACCENT,
                activebackground=CARD2, activeforeground="#04222b",
                font=fnt(9, "bold"), bd=0, padx=14, pady=5, cursor="hand2",
                relief="flat",
            )
            rb.pack(side="left", padx=2)
            self.src_buttons[value] = rb

        self.connect_btn = tk.Button(
            bar, text="เชื่อมต่อ", command=self.toggle_connection,
            bg=ACCENT, fg="#04222b", activebackground="#7dd3fc",
            activeforeground="#04222b", font=fnt(10, "bold"), bd=0,
            padx=20, pady=6, cursor="hand2", relief="flat",
        )
        self.connect_btn.pack(side="right", padx=12, pady=8)

        self.dynamic = tk.Frame(bar, bg=CARD)
        self.dynamic.pack(side="left", padx=(16, 0))

        self.port_var = tk.StringVar(value=self.args.port)
        self.server_var = tk.StringVar(value=self.args.server or "http://127.0.0.1:8000")
        self.port_combo: ttk.Combobox | None = None
        self.server_entry: tk.Entry | None = None

    def _build_content(self) -> None:
        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True, padx=18, pady=(8, 12))

        self.frames: dict[str, tk.Frame] = {}
        for key, _ in self.TABS:
            frame = tk.Frame(self.content, bg=BG)
            self.frames[key] = frame

        self._build_overview(self.frames["overview"])
        self._build_graphs(self.frames["graphs"])
        self._build_log(self.frames["log"])
        self._build_server(self.frames["server"])
        self._build_status(self.frames["status"])

    # -- overview ----------------------------------------------------------
    def _build_overview(self, parent: tk.Frame) -> None:
        badges = tk.Frame(parent, bg=BG)
        badges.pack(fill="x", pady=(0, 8))
        self.badge_usb = Badge(badges, "USB")
        self.badge_usb.pack(side="left", padx=(0, 6))
        self.badge_ap = Badge(badges, "AP")
        self.badge_ap.pack(side="left", padx=6)
        self.badge_uplink = Badge(badges, "Uplink")
        self.badge_uplink.pack(side="left", padx=6)
        self.badge_net = Badge(badges, "อินเทอร์เน็ต")
        self.badge_net.pack(side="left", padx=6)

        self.uptime_lbl = tk.Label(badges, text="อัปไทม์ -", bg=BG, fg=DIM,
                                   font=fnt(9))
        self.uptime_lbl.pack(side="right", padx=6)

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="both", expand=True)
        for r in range(2):
            grid.rowconfigure(r, weight=1, uniform="row")
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="col")

        self.card_cpu = StatCard(grid, "ความเร็ว CPU", ACCENT)
        self.card_cpu.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self.card_load = StatCard(grid, "ภาระ CPU", GREEN)
        self.card_load.grid(row=0, column=1, sticky="nsew", padx=6, pady=(0, 6))
        self.card_ap = StatCard(grid, "Access Point", PURPLE)
        self.card_ap.grid(row=0, column=2, sticky="nsew", padx=(6, 0), pady=(0, 6))
        self.card_sensor = StatCard(grid, "เซนเซอร์ ADC (GPIO34)", AMBER)
        self.card_sensor.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        self.card_quant = StatCard(grid, "การบีบอัดข้อมูล int8", GREEN)
        self.card_quant.grid(row=1, column=1, sticky="nsew", padx=6, pady=(6, 0))
        self.card_uplink = StatCard(grid, "Uplink / Heartbeat", RED)
        self.card_uplink.grid(row=1, column=2, sticky="nsew", padx=(6, 0), pady=(6, 0))

        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(fill="x", pady=(8, 0))
        self.led_card = StatCard(bottom, "สถานะไฟ LED (GPIO2)", ACCENT)
        self.led_card.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.link_card = StatCard(bottom, "รายละเอียดการเชื่อมต่อ", PURPLE)
        self.link_card.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.siphon_card = StatCard(bottom, "Siphon Queue (ส่งงานให้ PC)", GREEN)
        self.siphon_card.pack(side="left", fill="x", expand=True, padx=(6, 0))

    # -- graphs ------------------------------------------------------------
    def _build_graphs(self, parent: tk.Frame) -> None:
        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="both", expand=True)
        for c in (0, 1):
            grid.columnconfigure(c, weight=1, uniform="col")
        for r in (0, 1):
            grid.rowconfigure(r, weight=1, uniform="row")

        self.chart_clock = Chart(
            grid, "ความเร็ว CPU (MHz)", "MHz", y_min=50, y_max=260,
        )
        self.chart_clock.grid(row=0, column=0, sticky="nsew",
                              padx=(0, 6), pady=(0, 6))
        self.chart_load = Chart(
            grid, "ภาระ CPU (%)", "%", y_min=0, y_max=100,
            thresholds=(30, 70),
        )
        self.chart_load.grid(row=0, column=1, sticky="nsew",
                             padx=(6, 0), pady=(0, 6))

        self.chart_sensor = Chart(
            grid, "เซนเซอร์ ADC (mV) · ค่าจริงเทียบค่าที่แปลงกลับ", "mV",
            y_min=None, y_max=None,
        )
        self.chart_sensor.grid(row=1, column=0, sticky="nsew",
                               padx=(0, 6), pady=(6, 0))
        self.chart_link = Chart(
            grid, "สถานะลิงก์ (ตามเวลาจริง)", "",
            y_min=0, y_max=110,
            y_grid=(20, 60, 100),
            y_labels={20: "หลุด", 60: "uplink", 100: "เน็ตออก"},
        )
        self.chart_link.grid(row=1, column=1, sticky="nsew",
                             padx=(6, 0), pady=(6, 0))

        self.chart_siphon = Chart(
            parent, "Siphon Queue (ทยอยส่งงานให้ PC)", "งาน",
            y_min=None, y_max=None,
        )
        self.chart_siphon.pack(fill="both", expand=True, pady=(12, 0))

        stats = tk.Frame(parent, bg=BG)
        stats.pack(fill="x", pady=(8, 0))
        self.stat_cpu = tk.Label(stats, text="CPU: กำลังรอข้อมูล", bg=BG,
                                 fg=MUTED, font=fnt(9))
        self.stat_cpu.pack(side="left", padx=(0, 24))
        self.stat_sensor = tk.Label(stats, text="เซนเซอร์: กำลังรอข้อมูล",
                                    bg=BG, fg=MUTED, font=fnt(9))
        self.stat_sensor.pack(side="left", padx=(0, 24))
        self.stat_link = tk.Label(stats, text="ลิงก์: กำลังรอข้อมูล",
                                  bg=BG, fg=MUTED, font=fnt(9))
        self.stat_link.pack(side="left", padx=(0, 24))
        self.stat_siphon = tk.Label(stats, text="Siphon: กำลังรอข้อมูล",
                                    bg=BG, fg=MUTED, font=fnt(9))
        self.stat_siphon.pack(side="left")

    # -- log ---------------------------------------------------------------
    def _build_log(self, parent: tk.Frame) -> None:
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", pady=(0, 6))

        tk.Label(tools, text="ตัวกรอง", bg=BG, fg=MUTED,
                 font=fnt(9, "bold")).pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar(value="ทั้งหมด")
        combo = ttk.Combobox(tools, textvariable=self.filter_var, width=12,
                             values=("ทั้งหมด", "ข้อมูล", "เตือน", "ผิดพลาด"),
                             state="readonly", font=fnt(9))
        combo.pack(side="left", padx=(0, 12))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._render_log())

        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(tools, text="หยุดชั่วคราว", variable=self.pause_var,
                       bg=BG, fg=MUTED, selectcolor=CARD2, activebackground=BG,
                       activeforeground=FG, font=fnt(9)).pack(side="left", padx=6)

        for text, command in (("บันทึกไฟล์", self._save_log),
                              ("ล้าง", self._clear_log)):
            tk.Button(tools, text=text, command=command, bg=CARD2, fg=MUTED,
                      activebackground=CARD, activeforeground=FG, bd=0,
                      font=fnt(9, "bold"), padx=14, pady=4,
                      cursor="hand2").pack(side="right", padx=4)

        wrap = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        self.log = tk.Text(wrap, bg=CARD, fg="#c7d5ea", insertbackground=FG,
                           font=(THAI_FONT, 9), relief="flat", wrap="none",
                           selectbackground=CARD2)
        scroll = tk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.log.tag_configure("warn", foreground=AMBER)
        self.log.tag_configure("error", foreground=RED)
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.configure(state="disabled")

    # -- server ------------------------------------------------------------
    def _build_server(self, parent: tk.Frame) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=FG, rowheight=30, borderwidth=0,
                        font=(THAI_FONT, 10))
        style.configure("Treeview.Heading", background=CARD2, foreground=ACCENT,
                        font=(THAI_FONT, 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", CARD2)],
                  foreground=[("selected", FG)])

        cols = ("node", "online", "rssi", "ip", "firmware", "age")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings")
        headings = {
            "node": ("โหนด", 200), "online": ("ออนไลน์", 90),
            "rssi": ("RSSI", 90), "ip": ("IP", 170),
            "firmware": ("เฟิร์มแวร์", 170), "age": ("เห็นล่าสุด", 140),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.server_note = tk.Label(parent, text="", bg=BG, fg=MUTED,
                                    font=fnt(9), anchor="w")
        self.server_note.pack(fill="x", pady=(6, 0))

    # -- status (all) ------------------------------------------------------
    def _build_status(self, parent: tk.Frame) -> None:
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", pady=(0, 6))
        tk.Label(tools, text="สถานะระบบทั้งหมด (Real-time)", bg=BG, fg=FG,
                 font=fnt(12, "bold")).pack(side="left")
        self.status_refresh_btn = tk.Button(
            tools, text="รีเฟรช", command=self._refresh_status,
            bg=CARD2, fg=MUTED, activebackground=CARD, activeforeground=FG,
            bd=0, font=fnt(9, "bold"), padx=14, pady=4, cursor="hand2",
        )
        self.status_refresh_btn.pack(side="right", padx=4)

        wrap = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        self.status_text = tk.Text(wrap, bg=CARD, fg="#c7d5ea",
                                   font=(THAI_FONT, 10), relief="flat",
                                   wrap="none", state="disabled",
                                   selectbackground=CARD2)
        scroll = tk.Scrollbar(wrap, command=self.status_text.yview)
        self.status_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.status_text.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        self.status_text.tag_configure("header", foreground=ACCENT, font=(THAI_FONT, 11, "bold"))
        self.status_text.tag_configure("ok", foreground=GREEN)
        self.status_text.tag_configure("warn", foreground=AMBER)
        self.status_text.tag_configure("error", foreground=RED)
        self.status_text.tag_configure("value", foreground=FG)
        self.status_text.tag_configure("label", foreground=MUTED)

    def _refresh_status(self) -> None:
        st = self.state
        lines = []
        def add_header(text):
            lines.append(("header", text + "\\n"))
        def add_row(label, value, tag="value"):
            lines.append(("label", f"  {label}: "))
            lines.append((tag, f"{value}\\n"))
        def add_sep():
            lines.append(("label", "  " + "─" * 60 + "\\n"))

        add_header("═══ สถานะระบบทั้งหมด (FreeNet-X Node) ═══")
        add_sep()

        add_header("📡 การเชื่อมต่อ (Connectivity)")
        add_row("พอร์ต USB", st.port or "-")
        add_row("AP SSID", st.ap_ssid)
        add_row("AP IP", st.ap_ip)
        add_row("Uplink SSID", st.uplink_name)
        add_row("Uplink IP", st.uplink_ip)
        add_row("STA เชื่อมต่อ", "ใช่" if st.sta_connected else "ไม่")
        add_row("NAPT", "เปิด" if st.napt else "ยังไม่เปิด")
        add_row("NAPT Error", "มี" if st.napt_error else "ไม่มี", "error" if st.napt_error else "ok")
        add_row("DNS Server (AP)", st.dns_server)
        add_row("DNS Warning", "มี" if st.dns_warning else "ไม่มี", "warn" if st.dns_warning else "ok")
        add_row("เน็ต", {"online": "ออนไลน์", "offline": "ออฟไลน์"}.get(st.internet, "ยังไม่ทราบ"))
        add_row("WiFi Disconnect Reason", st.wifi_disconnect_reason if st.wifi_disconnect_reason else "-")
        add_row("RSSI (สัญญาณ)", f"{st.rssi} dBm" if st.rssi != 0 else "-")
        add_row("WiFi Channel", st.wifi_channel if st.wifi_channel > 0 else "-")
        add_row("Auth Mode", {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA3"}.get(st.authmode, "-"))
        add_sep()

        add_header("⚡ CPU / DFS")
        add_row("ความเร็ว CPU", f"{st.cpu_mhz} MHz")
        add_row("ภาระ CPU", f"{st.cpu_load} %")
        add_row("DFS เปิด", "ใช่" if st.dfs_enabled else "ไม่")
        add_row("DFS Tiers", st.dfs_tiers)
        add_row("ระดับ", "สูง" if st.cpu_mhz >= 240 else ("กลาง" if st.cpu_mhz >= 160 else "ประหยัด"))
        add_row("Free Heap", f"{st.free_heap} bytes" if st.free_heap > 0 else "-")
        add_row("Min Heap", f"{st.min_heap} bytes" if st.min_heap > 0 else "-")
        add_sep()

        add_header("📊 เซนเซอร์ ADC (GPIO34)")
        add_row("ADC พร้อม", "ใช่" if st.sensor_ready else "ไม่")
        add_row("GPIO", st.sensor_gpio if st.sensor_gpio >= 0 else "ปิด")
        add_row("เซนเซอร์ปิด", "ใช่" if st.sensor_disabled else "ไม่")
        add_row("ค่า ADC", f"{st.adc_mv} mV" if st.adc_mv is not None else "-")
        add_row("ค่า q (int8)", st.adc_q if st.adc_q is not None else "-")
        add_row("ค่าแปลงกลับ", f"{st.adc_restored:.1f} mV" if st.adc_restored is not None else "-")
        add_row("ความคลาดเคลื่อน", f"{st.adc_err:.1f} mV" if st.adc_err is not None else "-")
        add_row("ขนาดแพ็กเก็ต", f"{st.packet_bytes} B" if st.packet_bytes is not None else "-")
        add_sep()

        add_header("🔄 Siphon Queue")
        add_row("จำนวนที่ส่ง", f"{st.siphon_dispatched} งาน")
        add_row("จำนวนที่หลุด", f"{st.siphon_dropped} งาน", "warn" if st.siphon_dropped > 0 else "ok")
        add_row("ความจุคิว", f"{st.siphon_depth}")
        add_row("อัตราส่ง", "1 งาน / 100 ms")
        add_sep()

        add_header("🌐 Heartbeat / Config / Event")
        add_row("Heartbeat ครั้ง", st.heartbeat_count)
        add_row("Heartbeat HTTP", st.heartbeat_http if st.heartbeat_http is not None else "-")
        add_row("Config ครั้ง", st.config_count)
        add_row("Config HTTP", st.config_http if st.config_http is not None else "-")
        add_row("Event ครั้ง", st.event_count)
        add_row("Event HTTP", st.event_http if st.event_http is not None else "-")
        add_sep()

        add_header("⚠️ คำเตือน / ข้อผิดพลาด")
        add_row("Placeholder Uplink", "มี (ยังไม่ตั้งค่า)" if st.placeholder_uplink else "ไม่มี", "warn" if st.placeholder_uplink else "ok")
        add_row("Placeholder Token", "มี (ยังไม่ตั้งค่า)" if st.placeholder_token else "ไม่มี", "warn" if st.placeholder_token else "ok")
        add_row("จำนวน Error", st.error_count, "error" if st.error_count > 0 else "ok")
        add_row("จำนวน Warning", st.warning_count, "warn" if st.warning_count > 0 else "ok")
        add_row("Error ล่าสุด", st.last_error if st.last_error != "-" else "ไม่มี")
        add_sep()

        add_header("👥 ลูกข่าย (AP Clients)")
        if st.clients:
            for mac, ip in st.clients.items():
                add_row(mac, ip)
        else:
            add_row("ไม่มีลูกข่ายเชื่อมต่อ", "-")
        add_sep()

        add_header("💡 ไฟ LED (GPIO2)")
        led_text, led_reason = st.led_mode
        add_row("โหมดไฟ", led_text)
        add_row("เหตุผล", led_reason)
        add_row("GPIO", st.led_gpio)
        add_sep()

        add_header("⏱️ เวลา")
        add_row("อัปไทม์", f"{st.uptime_s} วินาที")
        add_row("Boot Reason", st.boot_reason)
        add_row("Boot Time", st.boot_at if hasattr(st, 'boot_at') else "-")
        add_sep()

        # Write to text widget
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        for tag, text in lines:
            self.status_text.insert("end", text, tag)
        self.status_text.configure(state="disabled")

    # -- tabs --------------------------------------------------------------
    def show_tab(self, key: str) -> None:
        self.active_tab = key
        for frame in self.frames.values():
            frame.pack_forget()
        self.frames[key].pack(fill="both", expand=True)
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(bg=CARD2 if active else BG,
                             fg=ACCENT if active else MUTED)
        if key == "graphs":
            self._refresh_graphs()
        if key == "status":
            self._refresh_status()

    # -- source switching --------------------------------------------------
    def _switch_source(self) -> None:
        for child in self.dynamic.winfo_children():
            child.destroy()
        if self.source_var.get() == "serial":
            tk.Label(self.dynamic, text="พอร์ต", bg=CARD, fg=MUTED,
                     font=fnt(9)).pack(side="left", padx=(0, 6))
            self.port_combo = ttk.Combobox(self.dynamic, textvariable=self.port_var,
                                           width=10, values=self._ports(),
                                           font=(THAI_FONT, 9))
            self.port_combo.pack(side="left")
            self.connect_btn.configure(
                text="ตัดการเชื่อมต่อ" if self.reader else "เชื่อมต่อ")
        else:
            tk.Label(self.dynamic, text="URL เซิร์ฟเวอร์", bg=CARD, fg=MUTED,
                     font=fnt(9)).pack(side="left", padx=(0, 6))
            tk.Entry(self.dynamic, textvariable=self.server_var, width=30,
                     bg=CARD2, fg=FG, insertbackground=FG, relief="flat",
                     font=(THAI_FONT, 9)).pack(side="left", ipady=4)
            self.connect_btn.configure(
                text="หยุดดึงข้อมูล" if self.poller else "เริ่มดึงข้อมูล")

    # -- helpers -----------------------------------------------------------
    def _ports(self) -> list[str]:
        if list_ports is None:
            return [self.args.port]
        found = [p.device for p in list_ports.comports()]
        if self.args.port not in found:
            found.insert(0, self.args.port)
        return found

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status_lbl.configure(text=text, fg=FG)
        self.status_dot.itemconfigure(self.status_dot_id, fill=color)

    # -- logging -----------------------------------------------------------
    def append_log(self, text: str) -> None:
        if "E (" in text or "ERROR" in text or "ผิดพลาด" in text:
            level = "error"
        elif "W (" in text or "WARN" in text:
            level = "warn"
        else:
            level = "info"
        self.log_lines.append((level, text))
        if len(self.log_lines) > 4000:
            del self.log_lines[:1000]
        if self.log_paused or not self._log_visible(level):
            return
        self._insert_log_line(level, text)

    def _log_visible(self, level: str) -> bool:
        choice = self.filter_var.get()
        return {
            "ทั้งหมด": True,
            "ข้อมูล": level == "info",
            "เตือน": level == "warn",
            "ผิดพลาด": level == "error",
        }.get(choice, True)

    def _insert_log_line(self, level: str, text: str) -> None:
        tag = "error" if level == "error" else ("warn" if level == "warn" else "")
        if "heartbeat HTTP=2" in text or "internet check" in text:
            tag = "ok"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 2500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for level, text in self.log_lines:
            if self._log_visible(level):
                self._insert_log_line(level, text)
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_lines.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="บันทึก log", defaultextension=".txt",
            filetypes=(("Text", "*.txt"), ("All", "*.*")),
            initialfile="freenetx-log.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for _level, text in self.log_lines:
                    fh.write(text + "\n")
            self._set_status(f"บันทึกแล้ว: {os.path.basename(path)}", GREEN)
        except OSError as exc:
            messagebox.showerror("บันทึกไม่สำเร็จ", str(exc))

    # -- connection --------------------------------------------------------
    def toggle_connection(self) -> None:
        if self.source_var.get() == "serial":
            if self.reader:
                self.disconnect_serial()
            else:
                self.connect_serial()
        else:
            if self.poller:
                self.disconnect_server()
            else:
                self.connect_server()

    def connect_serial(self) -> None:
        if serial is None:
            messagebox.showerror(
                "ไม่พบ pyserial",
                "โหมด USB ต้องใช้ pyserial\n\nติดตั้งด้วยคำสั่ง:\n    pip install pyserial",
            )
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("ยังไม่ได้เลือกพอร์ต", "เลือกพอร์ต COM ก่อนครับ")
            return
        self.state = NodeState(port=port)
        self.log_lines.clear()
        self._render_log()
        self.stop.clear()
        self.reader = SerialReader(port, self.args.baud, self.queue, self.stop)
        self.reader.start()
        write_monitor_lock(port)
        self.connect_btn.configure(text="ตัดการเชื่อมต่อ")

    def disconnect_serial(self) -> None:
        self.stop.set()
        clear_monitor_lock(self.state.port)
        self.reader = None
        self._set_status("ตัดการเชื่อมต่อแล้ว", MUTED)
        self.connect_btn.configure(text="เชื่อมต่อ")

    def connect_server(self) -> None:
        base = self.server_var.get().strip()
        if not base.startswith(("http://", "https://")):
            messagebox.showwarning("URL ไม่ถูกต้อง",
                                   "URL ต้องขึ้นต้นด้วย http:// หรือ https://")
            return
        self.stop.clear()
        self.poller = FleetPoller(base, self.queue, self.stop)
        self.poller.start()
        self._set_status("กำลังดึงข้อมูลจากเซิร์ฟเวอร์", ACCENT)
        self.connect_btn.configure(text="หยุดดึงข้อมูล")

    def disconnect_server(self) -> None:
        self.stop.set()
        self.poller = None
        self._set_status("หยุดดึงข้อมูลแล้ว", MUTED)
        self.connect_btn.configure(text="เริ่มดึงข้อมูล")

    # -- event pump --------------------------------------------------------
    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    changed = parse_line(payload, self.state)
                    self.append_log(payload)
                    if changed:
                        self._refresh_overview()
                        if self.active_tab == "status":
                            self._refresh_status()
                elif kind == "status":
                    self.append_log(f"[จอ] {payload}")
                    if payload.startswith("เชื่อมต่อแล้ว"):
                        self._set_status(payload, GREEN)
                    elif payload.startswith("ตัดการเชื่อมต่อ"):
                        self._set_status(payload, MUTED)
                        self.reader = None
                        self.connect_btn.configure(text="เชื่อมต่อ")
                    else:
                        self._set_status(payload, RED)
                        if payload.startswith("เปิดพอร์ตไม่สำเร็จ"):
                            clear_monitor_lock(self.state.port)
                elif kind == "fleet":
                    self._show_fleet(payload)
                elif kind == "fleet_error":
                    self.server_note.configure(text=f"เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: {payload}",
                                               fg=RED)
                    self._set_status("เซิร์ฟเวอร์ไม่ตอบสนอง", RED)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _tick(self) -> None:
        self.uptime_lbl.configure(text=f"อัปไทม์ {self.state.uptime_s} วินาที")
        if self.active_tab == "graphs":
            self._refresh_graphs()
        if self.active_tab == "status":
            self._refresh_status()
        self.root.after(1000, self._tick)

    # -- overview refresh --------------------------------------------------
    def _refresh_overview(self) -> None:
        st = self.state

        cpu_color = ACCENT if st.cpu_mhz >= 240 else (PURPLE if st.cpu_mhz >= 160 else GREEN)
        tier = "สูง" if st.cpu_mhz >= 240 else ("กลาง" if st.cpu_mhz >= 160 else "ประหยัด")
        self.card_cpu.set(
            f"{st.cpu_mhz} MHz" if st.cpu_mhz else "-",
            f"ระดับ{tier} · ปรับตามภาระงานอัตโนมัติ",
            cpu_color,
        )
        load_color = GREEN if st.cpu_load < 30 else (AMBER if st.cpu_load <= 70 else RED)
        target = 80 if st.cpu_load < 30 else (160 if st.cpu_load <= 70 else 240)
        self.card_load.set(
            f"{st.cpu_load} %" if st.cpu_mhz else "-",
            f"เป้าหมาย {target} MHz · วัดทุก 2 วินาที",
            load_color,
        )
        self.card_ap.set(
            st.ap_ssid,
            f"IP {st.ap_ip} · ลูกข่าย {len(st.clients)}/4 · LED GPIO {st.led_gpio}",
            PURPLE,
        )
        if st.adc_mv is None:
            self.card_sensor.set("-", "ยังไม่มีตัวอย่างจาก ADC", AMBER)
        else:
            self.card_sensor.set(
                f"{st.adc_mv} mV",
                f"ค่าดิบจาก ADC1 · q = {st.adc_q}",
                AMBER,
            )
        if st.adc_q is None:
            self.card_quant.set("-", "ยังไม่มีข้อมูล", GREEN)
        else:
            self.card_quant.set(
                f"{st.packet_bytes} ไบต์",
                f"{st.adc_mv} → {st.adc_restored:.1f} mV "
                f"(คลาดเคลื่อน {st.adc_err:.1f} mV)",
                GREEN,
            )
        if st.uplink_configured is False:
            self.card_uplink.set("ยังไม่ตั้งค่า", "โหมด AP อย่างเดียว", MUTED)
        elif st.sta_connected:
            hb = (f"heartbeat HTTP {st.heartbeat_http}"
                  if st.heartbeat_http is not None else "ยังไม่ส่ง heartbeat")
            self.card_uplink.set(st.uplink_name, f"IP {st.uplink_ip} · {hb}", GREEN)
        else:
            self.card_uplink.set(st.uplink_name, "กำลังเชื่อมต่อ uplink...", RED)

        led_text, led_reason = st.led_mode
        self.led_card.set(led_text, led_reason,
                          GREEN if led_text == "ติดค้าง" else AMBER)

        details = []
        details.append(f"NAPT: {'เปิด' if st.napt else 'ยังไม่เปิด'}")
        details.append(f"เน็ต: {self._internet_th()}")
        if st.config_http is not None:
            details.append(f"config HTTP {st.config_http}")
        if st.event_http is not None:
            details.append(f"event HTTP {st.event_http}")
        self.link_card.set(
            f"ลูกข่าย {len(st.clients)} เครื่อง" if st.clients else "ไม่มีลูกข่ายเชื่อมต่อ",
            " · ".join(details),
            PURPLE,
        )
        self.siphon_card.set(
            f"{st.siphon_dispatched} งาน",
            "ทยอยส่งผ่าน USB Serial (คิว 50) · ล่าสุด 1 งาน/100 มิลลิวินาที",
            GREEN,
        )

        self.badge_usb.set("USB เชื่อมต่อ" if self.state.port else "USB",
                           GREEN if self.state.port else DIM)
        self.badge_ap.set(f"AP {st.ap_ssid}" if st.ap_ssid != "-" else "AP",
                          GREEN if st.ap_ssid != "-" else DIM)
        self.badge_uplink.set(
            "Uplink พร้อม" if st.sta_connected else
            ("ไม่ได้ตั้งค่า" if st.uplink_configured is False else "Uplink รอเชื่อมต่อ"),
            GREEN if st.sta_connected else (DIM if st.uplink_configured is False else AMBER),
        )
        net_color = GREEN if st.internet == "online" else (RED if st.internet == "offline" else DIM)
        self.badge_net.set(f"เน็ต: {self._internet_th()}", net_color)

        self._refresh_graphs()

    def _internet_th(self) -> str:
        return {"online": "ออนไลน์", "offline": "ออฟไลน์"}.get(
            self.state.internet, "ยังไม่ทราบ")

    # -- graphs refresh ----------------------------------------------------
    def _refresh_graphs(self) -> None:
        st = self.state
        clock_points = [(t, float(mhz)) for t, mhz, _load in st.cpu_points]
        load_points = [(t, float(load)) for t, _mhz, load in st.cpu_points]
        self.chart_clock.set_data([
            {"name": "ความเร็ว", "color": ACCENT, "points": clock_points, "style": "step"},
        ])
        self.chart_load.set_data([
            {"name": "ภาระ", "color": PURPLE, "points": load_points},
        ])
        raw_points = [(t, float(mv)) for t, mv, _r in st.sensor_points]
        restored_points = [(t, float(r)) for t, _mv, r in st.sensor_points]
        self.chart_sensor.set_data([
            {"name": "ค่าจริง", "color": AMBER, "points": raw_points, "dots": True},
            {"name": "แปลงกลับ", "color": GREEN, "points": restored_points},
        ])
        link_points = [(t, float(level)) for t, level in st.link_points]
        link_color = GREEN if st.link_level == 100 else (AMBER if st.link_level == 60 else RED)
        self.chart_link.set_data([
            {"name": "สถานะ", "color": link_color, "points": link_points,
             "style": "step", "dots": True},
        ])
        siphon_points = [(t, float(n)) for t, n in st.siphon_points]
        self.chart_siphon.set_data([
            {"name": "ทยอยส่ง", "color": GREEN, "points": siphon_points, "dots": True},
        ])

        if st.cpu_points:
            loads = [p[2] for p in st.cpu_points]
            avg = sum(loads) / len(loads)
            self.stat_cpu.configure(
                text=f"CPU ภาระ: ต่ำสุด {min(loads)}% · เฉลี่ย {avg:.0f}% · "
                     f"สูงสุด {max(loads)}% · {len(loads)} ตัวอย่าง",
                fg=MUTED,
            )
        if st.sensor_points:
            errs = [abs(mv - r) for _t, mv, r in st.sensor_points]
            mvs = [mv for _t, mv, _r in st.sensor_points]
            self.stat_sensor.configure(
                text=f"เซนเซอร์: ต่ำสุด {min(mvs)} mV · สูงสุด {max(mvs)} mV · "
                     f"คลาดเคลื่อนเฉลี่ย {sum(errs) / len(errs):.2f} mV",
                fg=MUTED,
            )
        link_text = {100: "เน็ตออกปกติ", 60: "uplink ต่อได้ แต่ยังไม่ออกเน็ต",
                     20: "uplink หลุด"}.get(st.link_level, "ไม่ทราบสถานะ")
        self.stat_link.configure(
            text=f"ลิงก์: {link_text} · เปลี่ยนสถานะ {len(st.link_points)} ครั้ง",
            fg=MUTED,
        )
        self.stat_siphon.configure(
            text=f"Siphon: {st.siphon_dispatched} งาน · ทยอยส่ง 1 งาน/100ms · คิว 50",
            fg=MUTED,
        )

    # -- server table ------------------------------------------------------
    def _show_fleet(self, data: dict) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        nodes = data.get("nodes", [])
        for n in nodes:
            self.tree.insert("", "end", values=(
                n.get("node_id", "-"),
                "ออนไลน์" if n.get("online") else "ออฟไลน์",
                n.get("rssi") if n.get("rssi") is not None else "-",
                n.get("ip_address") or "-",
                n.get("firmware") or "-",
                f"{int(n.get('age_seconds', 0))} วินาที",
            ))
        self.server_note.configure(
            text=f"พบ {len(nodes)} โหนด · เวลาเซิร์ฟเวอร์ {data.get('server_time', '-')}",
            fg=MUTED,
        )
        self._set_status(f"ดึงข้อมูลสำเร็จ ({len(nodes)} โหนด)", GREEN)

    # -- demo --------------------------------------------------------------
    def _demo_tick(self) -> None:
        st = self.state
        self._demo_n += 1
        n = self._demo_n
        st.port = "DEMO"
        st.ap_ssid = "FreeNetX-Node"
        st.ap_ip = "192.168.4.1"
        st.led_gpio = "2"
        st.uplink_name = "DemoNet"
        st.uplink_configured = True
        st.sta_connected = True
        st.uplink_ip = "192.168.1.23"
        st.napt = True
        st.internet = "online"
        st.heartbeat_http = 200
        if n == 2:
            st.clients["AA:BB:CC:DD:EE:01"] = "192.168.4.2"
        load = int(45 + 40 * abs((n % 30) - 15) / 15)
        st.cpu_load = load
        st.cpu_mhz = 240 if load > 70 else (160 if load >= 30 else 80)
        now = time.monotonic()
        st.cpu_points.append((now, st.cpu_mhz, load))
        st.adc_mv = 1200 + int(600 * (1 + __import__("math").sin(n / 5)) / 2)
        st.adc_q = round(st.adc_mv / 3300 * 255) - 128
        st.adc_restored = ((st.adc_q + 128) / 255) * 3300
        st.adc_err = abs(st.adc_mv - st.adc_restored)
        st.packet_bytes = 6
        st.sensor_points.append((now, st.adc_mv, st.adc_restored))
        # จำลองสถานะลิงก์: หลุดช่วงสั้น ๆ ทุก ~20 วินาที เพื่อให้เห็นกราฟเปลี่ยนสี
        cycle = n % 24
        st.sta_connected = cycle not in (7, 8)
        st.internet = "online" if cycle not in (7, 8, 9) else "offline"
        st.link_points.append((now, st.link_level))
        st.siphon_dispatched += 1
        st.siphon_points.append((now, st.siphon_dispatched))
        st.dfs_enabled = True
        st.dfs_tiers = "80/160/240"
        st.sensor_ready = True
        st.sensor_gpio = 34
        st.napt = True
        st.heartbeat_count += 1
        st.config_count += 1
        st.rssi = -58 + (n % 10)
        st.wifi_channel = 6
        st.authmode = 3
        st.free_heap = 240000 + (n % 5000)
        st.min_heap = 230000
        st.boot_reason = "rst:0x1 (POWERON_RESET)"
        self._refresh_overview()
        self.root.after(900, self._demo_tick)

    def on_close(self) -> None:
        self.stop.set()
        clear_monitor_lock(self.state.port)
        self.root.destroy()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def headless(port: str, baud: int, seconds: int) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if serial is None:
        print("ต้องติดตั้ง pyserial: pip install pyserial", file=sys.stderr)
        return 2
    st = NodeState(port=port)
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"เปิด {port} ไม่สำเร็จ: {exc}", file=sys.stderr)
        return 2

    print(f"อ่านพอร์ต {port} @ {baud} เป็นเวลา {seconds} วินาที ...")
    end = time.time() + seconds
    last = 0.0
    try:
        while time.time() < end:
            raw = ser.readline()
            if raw:
                line = clean_line(raw.decode("utf-8", errors="replace"))
                if line:
                    parse_line(line, st)
                    print(line)
            if time.time() - last >= 2.0:
                last = time.time()
                print(
                    f"  >> CPU {st.cpu_mhz}MHz โหลด {st.cpu_load}% | "
                    f"ADC {st.adc_mv}mV q={st.adc_q} | AP {st.ap_ssid}/{st.ap_ip} | "
                    f"เน็ต {st.internet} | อัปไทม์ {st.uptime_s}s"
                )
    finally:
        ser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeNet-X PC Monitor (Thai UI)")
    ap.add_argument("--port", default="COM3", help="พอร์ต serial (ค่าเริ่มต้น COM3)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--server", default="",
                    help="URL ของ FastAPI สำหรับโหมดเซิร์ฟเวอร์")
    ap.add_argument("--demo", action="store_true", help="แสดงข้อมูลจำลอง")
    ap.add_argument("--headless", type=int, metavar="SECONDS",
                    help="ไม่เปิดหน้าต่าง อ่าน log แล้วพิมพ์สรุป")
    args = ap.parse_args()

    if args.headless:
        return headless(args.port, args.baud, args.headless)

    root = tk.Tk()
    app = MonitorApp(root, args)
    if not args.demo:
        if app.source_var.get() == "serial":
            root.after(400, app.connect_serial)
        else:
            root.after(400, app.connect_server)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
