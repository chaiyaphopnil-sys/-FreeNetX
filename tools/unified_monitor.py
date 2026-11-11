#!/usr/bin/env python3
"""FreeNet-X Unified Monitor (Thai UI, Beautiful Graphs).

โปรแกรมแสดงผลครบวงจร: ภาพรวม + กราฟ beautiful + บันทึก + เซิร์ฟเวอร์ + สถานะทั้งหมด
ในโปรแกรมหน้าเดียว

    python unified_monitor.py                  # GUI, serial COM3
    python unified_monitor.py --demo           # ข้อมูลจำลอง
    python unified_monitor.py --port COM5      # พอร์ตอื่น
    python unified_monitor.py --server http://192.168.1.10:8000
"""
from __future__ import annotations

import argparse
import json
import math
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

sys.path.insert(0, r"C:\Users\aisim\Desktop\FreeNetX\FreeNetX_Autonomous_Intelligence_V2\tools")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

# ---------------------------------------------------------------------------
# Theme (Beautiful Dark)
# ---------------------------------------------------------------------------
BG = "#050a14"
SURFACE = "#0a1220"
CARD = "#0f1a2e"
CARD2 = "#162540"
BORDER = "#1e3a5f"
FG = "#f0f6ff"
MUTED = "#7a9cc4"
DIM = "#4a6080"
ACCENT = "#00e5ff"
PURPLE = "#b388ff"
GREEN = "#00e676"
AMBER = "#ffab00"
RED = "#ff5252"
CYAN = "#18ffff"
PINK = "#ff4081"

MAX_POINTS = 120
THAI_FONT = "Leelawadee UI"


def fnt(size=10, weight="normal"):
    return (THAI_FONT, size, weight)


def pick_font(root):
    try:
        families = set(tkfont.families(root))
    except Exception:
        return "TkDefaultFont"
    for name in ("Leelawadee UI", "Leelawadee", "Sarabun", "Noto Sans Thai",
                 "Segoe UI", "Tahoma"):
        if name in families:
            return name
    return "TkDefaultFont"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
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
UPLINK_RE = re.compile(r"AP=(\S+) uplink=(.+)$")
STA_IP_RE = re.compile(r"STA got IP ([\d.]+)")
STA_DISC_RE = re.compile(r"STA disconnected")
RECONNECT_RE = re.compile(r"Reconnecting to uplink")
NAPT_RE = re.compile(r"NAPT enabled")
CLIENT_CONN_RE = re.compile(r"Client connected: ([0-9A-Fa-f:]{17})")
CLIENT_DISC_RE = re.compile(r"Client disconnected: ([0-9A-Fa-f:]{17})")
CLIENT_IP_RE = re.compile(r"AP client got IP ([\d.]+) \(([0-9A-Fa-f:]{17})\)")
SIPHON_RE = re.compile(r"SEND_TO_PC:(\d+):(\S+)")
NAPT_FAIL_RE = re.compile(r"NAPT enable failed")
DNS_WARN_RE = re.compile(r"Uplink has no usable IPv4 DNS")
DNS_INFO_RE = re.compile(r"AP DHCP DNS -> ([\d.]+)")
SENSOR_READY_RE = re.compile(r"\[SENSOR\] ADC1 GPIO(\d+) ready")
SENSOR_DISABLED_RE = re.compile(r"\[SENSOR\] (ADC sampling disabled|GPIO\d+ is not an ADC1 pin)")
DFS_ENABLED_RE = re.compile(r"\[CPU\] DFS enabled: (\d+)/(\d+)/(\d+) MHz")
PLACEHOLDER_UPLINK_RE = re.compile(r"UPLINK_SSID is still a placeholder")
PLACEHOLDER_TOKEN_RE = re.compile(r"NODE_TOKEN is still a placeholder")
SIPHON_DROP_RE = re.compile(r"\[Siphon\] Queue full, dropping task")
SIPHON_INIT_RE = re.compile(r"Siphon.*Queue initialized.*depth=(\d+)")
WIFI_REASON_RE = re.compile(r"STA disconnected \(reason=(\d+)\)")
RSSI_RE = re.compile(r"STA RSSI=(-?\d+) dBm, channel=(\d+), authmode=(\d+)")
MEM_RE = re.compile(r"\[MEM\] free heap=(\d+) bytes, min=(\d+)")
BOOT_REASON_RE = re.compile(r"^(rst:0x\w+) \(([^)]+)\)")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
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
    boot_at: float = field(default_factory=time.monotonic)
    cpu_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    sensor_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    link_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    siphon_points: deque = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
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
    siphon_dispatched: int = 0
    siphon_dropped: int = 0
    siphon_depth: int = 50
    wifi_disconnect_reason: int = 0
    heartbeat_count: int = 0
    config_count: int = 0
    event_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    last_error: str = "-"
    rssi: int = 0
    wifi_channel: int = 0
    authmode: int = 0
    free_heap: int = 0
    min_heap: int = 0
    boot_reason: str = "-"

    @property
    def uptime_s(self) -> int:
        return max(0, int(time.monotonic() - self.boot_at))

    @property
    def link_level(self) -> int:
        if self.internet == "online":
            return 100
        if self.sta_connected:
            return 60
        return 20

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
        st.min_heap = 0
        st.rssi = 0
        st.wifi_channel = 0
        st.authmode = 0
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
        st.heartbeat_count += 1
        changed = True

    m = CFG_RE.search(line)
    if m:
        st.config_http = int(m.group(1))
        st.config_count += 1
        changed = True

    m = EVENT_RE.search(line)
    if m:
        st.event_http = int(m.group(1))
        st.event_count += 1
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

    if "E (" in line or "ERROR" in line.upper():
        st.error_count += 1
        st.last_error = line[:80]
        changed = True
    elif "W (" in line or "WARN" in line.upper():
        st.warning_count += 1
        changed = True

    return changed


# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------
class SerialReader(threading.Thread):
    def __init__(self, port, baud, out, stop):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.out = out
        self.stop = stop

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.2)
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass
        except Exception as exc:
            self.out.put(("status", f"เปิดพอร์ตไม่สำเร็จ: {exc}"))
            return
        self.out.put(("status", f"เชื่อมต่อแล้ว {self.port}"))
        try:
            while not self.stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                line = clean_line(raw.decode("utf-8", errors="replace"))
                if line:
                    self.out.put(("line", line))
        except Exception as exc:
            self.out.put(("status", f"อ่านข้อมูลผิดพลาด: {exc}"))
        finally:
            try:
                ser.close()
            except Exception:
                pass
            self.out.put(("status", "ตัดการเชื่อมต่อแล้ว"))


class FleetPoller(threading.Thread):
    def __init__(self, base, out, stop):
        super().__init__(daemon=True)
        self.base = base.rstrip("/")
        self.out = out
        self.stop = stop

    def run(self):
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.base + "/v1/fleet", timeout=4) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.out.put(("fleet", data))
            except Exception as exc:
                self.out.put(("fleet_error", str(exc)))
            self.stop.wait(3.0)


# ---------------------------------------------------------------------------
# Beautiful Graph Canvas
# ---------------------------------------------------------------------------
class BeautifulGraph(tk.Canvas):
    def __init__(self, parent, title, subtitle="", color=ACCENT,
                 y_min=None, y_max=None, unit="", height=160):
        super().__init__(parent, bg=CARD, highlightthickness=0, bd=0,
                         height=height)
        self.title = title
        self.subtitle = subtitle
        self.color = color
        self.y_min = y_min
        self.y_max = y_max
        self.unit = unit
        self.points = deque(maxlen=MAX_POINTS)
        self.bind("<Configure>", lambda e: self._draw())

    def set_points(self, pts):
        self.points = deque(pts, maxlen=MAX_POINTS)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 50 or h < 50:
            return

        for i in range(0, h, 2):
            ratio = i / h
            r = int(15 + ratio * 5)
            g = int(26 + ratio * 8)
            b = int(46 + ratio * 12)
            self.create_line(0, i, w, i, fill=f"#{r:02x}{g:02x}{b:02x}")

        self.create_text(12, 12, text=self.title, anchor="w", fill=FG,
                         font=fnt(11, "bold"))
        if self.subtitle:
            self.create_text(12, 30, text=self.subtitle, anchor="w",
                             fill=MUTED, font=fnt(8))

        left, right, top, bottom = 44, 12, 44, 20
        plot_w = w - left - right
        plot_h = h - top - bottom
        if plot_w < 20 or plot_h < 20:
            return

        pts = list(self.points)
        if not pts:
            self.create_text(w // 2, h // 2, text="กำลังรอข้อมูล...",
                             fill=DIM, font=fnt(10))
            return

        t_min = pts[0][0]
        t_max = pts[-1][0]
        span = t_max - t_min
        if span <= 0:
            span = 1.0

        values = [p[1] for p in pts]
        lo = self.y_min if self.y_min is not None else min(values)
        hi = self.y_max if self.y_max is not None else max(values)
        if self.y_max is None:
            pad = max((hi - lo) * 0.15, 1.0)
            lo -= pad
            hi += pad
        if hi <= lo:
            hi = lo + 1

        def x_of(t):
            return left + (t - t_min) / span * plot_w

        def y_of(v):
            clamped = min(max(v, lo), hi)
            return top + (1 - (clamped - lo) / (hi - lo)) * plot_h

        for i in range(5):
            val = lo + (hi - lo) * i / 4
            y = y_of(val)
            self.create_line(left, y, left + plot_w, y, fill="#1a2d4a",
                             dash=(2, 4))
            self.create_text(left - 6, y, text=f"{val:.0f}", anchor="e",
                             fill=DIM, font=fnt(7))

        self.create_text(left, top + plot_h + 12, text=f"-{span:.0f}s",
                         anchor="w", fill=DIM, font=fnt(7))
        self.create_text(left + plot_w, top + plot_h + 12, text="ล่าสุด",
                         anchor="e", fill=DIM, font=fnt(7))
        if self.unit:
            self.create_text(left + plot_w + 2, top - 2, text=self.unit,
                             anchor="w", fill=MUTED, font=fnt(7, "bold"))

        coords = []
        for t, v in pts:
            coords.extend((x_of(t), y_of(v)))
        if len(coords) >= 4:
            self.create_line(*coords, fill=self.color, width=5,
                             smooth=True, splinesteps=12)
            self.create_line(*coords, fill=self.color, width=2,
                             smooth=True, splinesteps=12)

        for t, v in pts[-6:]:
            x, y = x_of(t), y_of(v)
            self.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                             fill=self.color, outline=BG, width=1)

        if pts:
            last_val = pts[-1][1]
            badge_x = left + plot_w - 2
            badge_y = top + 2
            self.create_rectangle(badge_x - 44, badge_y - 10,
                                  badge_x + 2, badge_y + 10,
                                  fill=self.color, outline="")
            self.create_text(badge_x - 21, badge_y, text=f"{last_val:.0f}",
                             fill="#050a14", font=fnt(9, "bold"))

        self.create_rectangle(0, 0, w - 1, h - 1, outline=BORDER, width=1)


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------
class StatCard(tk.Frame):
    def __init__(self, parent, title, color=ACCENT):
        super().__init__(parent, bg=BORDER, bd=0, highlightthickness=0)
        inner = tk.Frame(self, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(inner, bg=color, width=3).pack(side="left", fill="y")
        body = tk.Frame(inner, bg=CARD)
        body.pack(side="left", fill="both", expand=True, padx=10, pady=6)
        self.title = tk.Label(body, text=title, bg=CARD, fg=MUTED,
                              font=fnt(8, "bold"))
        self.title.pack(anchor="w")
        self.value = tk.Label(body, text="-", bg=CARD, fg=FG,
                              font=fnt(16, "bold"))
        self.value.pack(anchor="w", pady=(1, 0))
        self.sub = tk.Label(body, text="", bg=CARD, fg=DIM, font=fnt(7),
                            justify="left", anchor="w")
        self.sub.pack(anchor="w", fill="x")
        self.accent = color

    def set(self, value, sub="", color=None):
        self.value.configure(text=value, fg=color or FG)
        self.sub.configure(text=sub)


class Badge(tk.Frame):
    def __init__(self, parent, label):
        super().__init__(parent, bg=CARD2, highlightbackground=BORDER,
                         highlightthickness=1)
        self.dot = tk.Canvas(self, width=10, height=10, bg=CARD2,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(8, 3), pady=6)
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill=DIM, outline="")
        self.label = tk.Label(self, text=label, bg=CARD2, fg=MUTED,
                              font=fnt(8))
        self.label.pack(side="left", padx=(0, 8))

    def set(self, text, color):
        self.label.configure(text=text, fg=FG)
        self.dot.itemconfigure(self.dot_id, fill=color)


# ---------------------------------------------------------------------------
# Monitor App
# ---------------------------------------------------------------------------
class MonitorApp:
    TABS = (
        ("overview", "ภาพรวม"),
        ("graphs", "กราฟ"),
        ("log", "บันทึก"),
        ("server", "เซิร์ฟเวอร์"),
        ("status", "สถานะทั้งหมด"),
    )

    def __init__(self, root, args):
        global THAI_FONT
        self.root = root
        self.args = args
        self.state = NodeState()
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.reader = None
        self.poller = None
        self.log_lines = []
        self.log_paused = False
        self._demo_n = 0
        self.active_tab = "overview"

        THAI_FONT = pick_font(root)
        root.title("FreeNet-X · ศูนย์ควบคุมโหนด")
        root.configure(bg=BG)
        root.geometry("1280x860")
        root.minsize(1000, 700)

        self._build_header()
        self._build_nav()
        self._build_connbar()
        self._build_content()
        self._build_tabs()
        self.show_tab("overview")

        self.source_var.trace_add("write", lambda *_: self._switch_source())
        self._switch_source()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain_queue)
        root.after(1000, self._tick)

        if args.demo:
            root.after(400, self._demo_tick)

    def _build_header(self):
        header = tk.Frame(self.root, bg=SURFACE, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)

        canvas = tk.Canvas(header, width=32, height=32, bg=SURFACE,
                           highlightthickness=0)
        canvas.pack(side="left", padx=(18, 10), pady=10)
        canvas.create_rectangle(2, 2, 30, 30, fill=ACCENT, outline="")
        canvas.create_text(16, 16, text="FX", fill="#050a14",
                           font=fnt(11, "bold"))

        tk.Label(header, text="ศูนย์ควบคุม FreeNet-X", bg=SURFACE, fg=FG,
                 font=fnt(13, "bold")).pack(side="left", pady=8)
        tk.Label(header, text="มอนิเตอร์บอร์ด ESP32 · ภาษาไทย · Real-time",
                 bg=SURFACE, fg=MUTED, font=fnt(8)).pack(side="left",
                                                          padx=(10, 0),
                                                          pady=8)

        self.status_pill = tk.Frame(header, bg=CARD2,
                                    highlightbackground=BORDER,
                                    highlightthickness=1)
        self.status_pill.pack(side="right", padx=16)
        self.status_dot = tk.Canvas(self.status_pill, width=10, height=10,
                                    bg=CARD2, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(8, 3), pady=7)
        self.status_dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=DIM,
                                                         outline="")
        self.status_lbl = tk.Label(self.status_pill, text="ยังไม่เชื่อมต่อ",
                                   bg=CARD2, fg=MUTED, font=fnt(8))
        self.status_lbl.pack(side="left", padx=(0, 8))

    def _build_nav(self):
        self.nav = tk.Frame(self.root, bg=BG)
        self.nav.pack(fill="x", padx=16, pady=(8, 0))
        self.nav_buttons = {}
        for key, label in self.TABS:
            btn = tk.Button(self.nav, text=label,
                            command=lambda k=key: self.show_tab(k),
                            bg=BG, fg=MUTED, activebackground=CARD2,
                            activeforeground=FG, font=fnt(9, "bold"), bd=0,
                            padx=14, pady=6, cursor="hand2", relief="flat")
            btn.pack(side="left", padx=(0, 3))
            self.nav_buttons[key] = btn

    def _build_connbar(self):
        bar = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        bar.pack(fill="x", padx=16, pady=(6, 0))

        tk.Label(bar, text="แหล่งข้อมูล", bg=CARD, fg=MUTED,
                 font=fnt(8, "bold")).pack(side="left", padx=(10, 6), pady=6)

        self.source_var = tk.StringVar(
            value="server" if self.args.server else "serial")
        for text, value in (("USB", "serial"), ("เซิร์ฟเวอร์", "server")):
            rb = tk.Radiobutton(bar, text=text, value=value,
                                variable=self.source_var, indicatoron=False,
                                bg=CARD2, fg=MUTED, selectcolor=ACCENT,
                                activebackground=CARD2, activeforeground="#050a14",
                                font=fnt(8, "bold"), bd=0, padx=10, pady=4,
                                cursor="hand2", relief="flat")
            rb.pack(side="left", padx=2, pady=6)

        self.connect_btn = tk.Button(bar, text="เชื่อมต่อ",
                                     command=self.toggle_connection,
                                     bg=ACCENT, fg="#050a14",
                                     activebackground="#7dd3fc",
                                     font=fnt(9, "bold"), bd=0, padx=16,
                                     pady=5, cursor="hand2", relief="flat")
        self.connect_btn.pack(side="right", padx=10, pady=6)

        self.dynamic = tk.Frame(bar, bg=CARD)
        self.dynamic.pack(side="left", padx=(12, 0))

        self.port_var = tk.StringVar(value=self.args.port)
        self.server_var = tk.StringVar(value=self.args.server or "http://127.0.0.1:8000")
        self.port_combo = None
        self.server_entry = None

    def _build_content(self):
        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True, padx=16, pady=(6, 10))

        self.frames = {}
        for key, _ in self.TABS:
            frame = tk.Frame(self.content, bg=BG)
            self.frames[key] = frame

        self._build_overview(self.frames["overview"])
        self._build_graphs(self.frames["graphs"])
        self._build_log(self.frames["log"])
        self._build_server(self.frames["server"])
        self._build_status(self.frames["status"])

    def _build_overview(self, parent):
        badges = tk.Frame(parent, bg=BG)
        badges.pack(fill="x", pady=(0, 6))
        self.badge_usb = Badge(badges, "USB")
        self.badge_usb.pack(side="left", padx=(0, 4))
        self.badge_ap = Badge(badges, "AP")
        self.badge_ap.pack(side="left", padx=4)
        self.badge_uplink = Badge(badges, "Uplink")
        self.badge_uplink.pack(side="left", padx=4)
        self.badge_net = Badge(badges, "อินเทอร์เน็ต")
        self.badge_net.pack(side="left", padx=4)
        self.uptime_lbl = tk.Label(badges, text="อัปไทม์ -", bg=BG, fg=DIM,
                                   font=fnt(8))
        self.uptime_lbl.pack(side="right", padx=6)

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="both", expand=True)
        for r in range(2):
            grid.rowconfigure(r, weight=1, uniform="row")
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="col")

        self.card_cpu = StatCard(grid, "ความเร็ว CPU", ACCENT)
        self.card_cpu.grid(row=0, column=0, sticky="nsew", padx=(0, 4),
                           pady=(0, 4))
        self.card_load = StatCard(grid, "ภาระ CPU", PURPLE)
        self.card_load.grid(row=0, column=1, sticky="nsew", padx=4,
                            pady=(0, 4))
        self.card_ap = StatCard(grid, "Access Point", GREEN)
        self.card_ap.grid(row=0, column=2, sticky="nsew", padx=(4, 0),
                          pady=(0, 4))
        self.card_sensor = StatCard(grid, "เซนเซอร์ ADC", AMBER)
        self.card_sensor.grid(row=1, column=0, sticky="nsew", padx=(0, 4),
                              pady=(4, 0))
        self.card_quant = StatCard(grid, "การบีบอัด int8", CYAN)
        self.card_quant.grid(row=1, column=1, sticky="nsew", padx=4,
                             pady=(4, 0))
        self.card_uplink = StatCard(grid, "Uplink / Heartbeat", RED)
        self.card_uplink.grid(row=1, column=2, sticky="nsew", padx=(4, 0),
                              pady=(4, 0))

        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(fill="x", pady=(6, 0))
        self.led_card = StatCard(bottom, "สถานะไฟ LED (GPIO2)", ACCENT)
        self.led_card.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.link_card = StatCard(bottom, "รายละเอียดการเชื่อมต่อ", PURPLE)
        self.link_card.pack(side="left", fill="x", expand=True, padx=4)
        self.siphon_card = StatCard(bottom, "Siphon Queue", GREEN)
        self.siphon_card.pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _build_graphs(self, parent):
        container = tk.Frame(parent, bg=BG)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        self.chart_cpu = BeautifulGraph(
            container, "ความเร็ว CPU (MHz)", "DFS: 80 → 160 → 240 MHz",
            color=ACCENT, y_min=60, y_max=260, unit="MHz")
        self.chart_cpu.grid(row=0, column=0, sticky="nsew", padx=(0, 4),
                            pady=(0, 4))

        self.chart_load = BeautifulGraph(
            container, "ภาระ CPU (%)", "Threshold: 30% / 70%",
            color=PURPLE, y_min=0, y_max=100, unit="%")
        self.chart_load.grid(row=0, column=1, sticky="nsew", padx=(4, 0),
                             pady=(0, 4))

        self.chart_adc = BeautifulGraph(
            container, "เซนเซอร์ ADC (mV)", "ค่าจริง vs แปลงกลับ",
            color=AMBER, y_min=0, y_max=3500, unit="mV")
        self.chart_adc.grid(row=1, column=0, sticky="nsew", padx=(0, 4),
                            pady=(4, 0))

        self.chart_siphon = BeautifulGraph(
            container, "Siphon Queue (งาน)", "ทยอยส่งให้ PC",
            color=GREEN, y_min=0, y_max=None, unit="งาน")
        self.chart_siphon.grid(row=1, column=1, sticky="nsew", padx=(4, 0),
                               pady=(4, 0))

    def _build_log(self, parent):
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", pady=(0, 6))

        tk.Label(tools, text="ตัวกรอง", bg=BG, fg=MUTED,
                 font=fnt(8, "bold")).pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar(value="ทั้งหมด")
        combo = ttk.Combobox(tools, textvariable=self.filter_var, width=10,
                             values=("ทั้งหมด", "ข้อมูล", "เตือน", "ผิดพลาด"),
                             state="readonly", font=fnt(8))
        combo.pack(side="left", padx=(0, 10))
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_log())

        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(tools, text="หยุดชั่วคราว", variable=self.pause_var,
                       bg=BG, fg=MUTED, selectcolor=CARD2, activebackground=BG,
                       activeforeground=FG, font=fnt(8)).pack(side="left",
                                                             padx=6)

        for text, cmd in (("บันทึกไฟล์", self._save_log),
                          ("ล้าง", self._clear_log)):
            tk.Button(tools, text=text, command=cmd, bg=CARD2, fg=MUTED,
                      activebackground=CARD, activeforeground=FG, bd=0,
                      font=fnt(8, "bold"), padx=10, pady=3,
                      cursor="hand2").pack(side="right", padx=3)

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

    def _build_server(self, parent):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=FG, rowheight=28, borderwidth=0,
                        font=(THAI_FONT, 9))
        style.configure("Treeview.Heading", background=CARD2,
                        foreground=ACCENT, font=(THAI_FONT, 8, "bold"),
                        borderwidth=0)
        style.map("Treeview", background=[("selected", CARD2)],
                  foreground=[("selected", FG)])

        cols = ("node", "online", "rssi", "ip", "firmware", "age")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings")
        headings = {
            "node": ("โหนด", 180), "online": ("ออนไลน์", 80),
            "rssi": ("RSSI", 80), "ip": ("IP", 160),
            "firmware": ("เฟิร์มแวร์", 160), "age": ("เห็นล่าสุด", 130),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.server_note = tk.Label(parent, text="", bg=BG, fg=MUTED,
                                    font=fnt(8), anchor="w")
        self.server_note.pack(fill="x", pady=(4, 0))

    def _build_status(self, parent):
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", pady=(0, 6))
        tk.Label(tools, text="สถานะระบบทั้งหมด (Real-time)", bg=BG, fg=FG,
                 font=fnt(11, "bold")).pack(side="left")
        tk.Button(tools, text="รีเฟรช", command=self._refresh_status,
                  bg=CARD2, fg=MUTED, activebackground=CARD,
                  activeforeground=FG, bd=0, font=fnt(8, "bold"),
                  padx=12, pady=3, cursor="hand2").pack(side="right", padx=4)

        wrap = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        self.status_text = tk.Text(wrap, bg=CARD, fg="#c7d5ea",
                                   font=(THAI_FONT, 9), relief="flat",
                                   wrap="none", state="disabled",
                                   selectbackground=CARD2)
        scroll = tk.Scrollbar(wrap, command=self.status_text.yview)
        self.status_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.status_text.pack(side="left", fill="both", expand=True,
                             padx=8, pady=8)
        for tag, color, size, weight in [
            ("header", ACCENT, 10, "bold"), ("ok", GREEN, 9, "normal"),
            ("warn", AMBER, 9, "normal"), ("error", RED, 9, "normal"),
            ("value", FG, 9, "normal"), ("label", MUTED, 9, "normal"),
        ]:
            self.status_text.tag_configure(tag, foreground=color,
                                          font=(THAI_FONT, size, weight))

    def _build_tabs(self):
        pass

    def show_tab(self, key):
        self.active_tab = key
        for frame in self.frames.values():
            frame.pack_forget()
        self.frames[key].pack(fill="both", expand=True)
        for name, btn in self.nav_buttons.items():
            active = name == key
            btn.configure(bg=CARD2 if active else BG,
                          fg=ACCENT if active else MUTED)
        if key == "graphs":
            self._update_graphs()
        if key == "status":
            self._refresh_status()

    # -- connection --------------------------------------------------------
    def _switch_source(self):
        for child in self.dynamic.winfo_children():
            child.destroy()
        if self.source_var.get() == "serial":
            tk.Label(self.dynamic, text="พอร์ต", bg=CARD, fg=MUTED,
                     font=fnt(8)).pack(side="left", padx=(0, 6))
            self.port_combo = ttk.Combobox(self.dynamic,
                                           textvariable=self.port_var,
                                           width=8, values=self._ports(),
                                           font=(THAI_FONT, 8))
            self.port_combo.pack(side="left")
            self.connect_btn.configure(
                text="ตัดการเชื่อมต่อ" if self.reader else "เชื่อมต่อ")
        else:
            tk.Label(self.dynamic, text="URL", bg=CARD, fg=MUTED,
                     font=fnt(8)).pack(side="left", padx=(0, 6))
            tk.Entry(self.dynamic, textvariable=self.server_var, width=24,
                     bg=CARD2, fg=FG, insertbackground=FG, relief="flat",
                     font=(THAI_FONT, 8)).pack(side="left", ipady=3)
            self.connect_btn.configure(
                text="หยุดดึงข้อมูล" if self.poller else "เริ่มดึงข้อมูล")

    def _ports(self):
        if list_ports is None:
            return [self.args.port]
        found = [p.device for p in list_ports.comports()]
        if self.args.port not in found:
            found.insert(0, self.args.port)
        return found

    def _set_status(self, text, color=MUTED):
        self.status_lbl.configure(text=text, fg=FG)
        self.status_dot.itemconfigure(self.status_dot_id, fill=color)

    # -- logging -----------------------------------------------------------
    def append_log(self, text):
        if "E (" in text or "ERROR" in text.upper():
            level = "error"
        elif "W (" in text or "WARN" in text.upper():
            level = "warn"
        else:
            level = "info"
        self.log_lines.append((level, text))
        if len(self.log_lines) > 4000:
            del self.log_lines[:1000]
        if self.log_paused or not self._log_visible(level):
            return
        self._insert_log_line(level, text)

    def _log_visible(self, level):
        choice = self.filter_var.get()
        return {
            "ทั้งหมด": True, "ข้อมูล": level == "info",
            "เตือน": level == "warn", "ผิดพลาด": level == "error",
        }.get(choice, True)

    def _insert_log_line(self, level, text):
        tag = "error" if level == "error" else ("warn" if level == "warn" else "")
        if "heartbeat HTTP=2" in text or "internet check" in text:
            tag = "ok"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 2500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for level, text in self.log_lines:
            if self._log_visible(level):
                self._insert_log_line(level, text)
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log_lines.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            title="บันทึก log", defaultextension=".txt",
            filetypes=(("Text", "*.txt"), ("All", "*.*")),
            initialfile="freenetx-log.txt")
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
    def toggle_connection(self):
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

    def connect_serial(self):
        if serial is None:
            messagebox.showerror("ไม่พบ pyserial",
                                 "โหมด USB ต้องใช้ pyserial\n\npip install pyserial")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("ยังไม่ได้เลือกพอร์ต", "เลือกพอร์ต COM ก่อน")
            return
        self.state = NodeState(port=port)
        self.log_lines.clear()
        self._render_log()
        self.stop.clear()
        self.reader = SerialReader(port, self.args.baud, self.queue, self.stop)
        self.reader.start()
        self._write_lock(port)
        self.connect_btn.configure(text="ตัดการเชื่อมต่อ")

    def disconnect_serial(self):
        self.stop.set()
        self._clear_lock(self.state.port)
        self.reader = None
        self._set_status("ตัดการเชื่อมต่อแล้ว", MUTED)
        self.connect_btn.configure(text="เชื่อมต่อ")

    def connect_server(self):
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

    def disconnect_server(self):
        self.stop.set()
        self.poller = None
        self._set_status("หยุดดึงข้อมูลแล้ว", MUTED)
        self.connect_btn.configure(text="เริ่มดึงข้อมูล")

    def _write_lock(self, port):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", port)
        path = os.path.join(tempfile.gettempdir(),
                            f"freenetx-monitor-{safe}.lock")
        try:
            with open(path, "w", encoding="ascii") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    def _clear_lock(self, port):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", port)
        path = os.path.join(tempfile.gettempdir(),
                            f"freenetx-monitor-{safe}.lock")
        try:
            os.remove(path)
        except OSError:
            pass

    # -- event pump --------------------------------------------------------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    changed = parse_line(payload, self.state)
                    self.append_log(payload)
                    if changed:
                        self._refresh_overview()
                        self._update_graphs()
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
                            self._clear_lock(self.state.port)
                elif kind == "fleet":
                    self._show_fleet(payload)
                elif kind == "fleet_error":
                    self.server_note.configure(
                        text=f"เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: {payload}", fg=RED)
                    self._set_status("เซิร์ฟเวอร์ไม่ตอบสนอง", RED)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _tick(self):
        self.uptime_lbl.configure(
            text=f"อัปไทม์ {self.state.uptime_s} วินาที")
        self._update_graphs()
        if self.active_tab == "status":
            self._refresh_status()
        self.root.after(1000, self._tick)

    # -- overview refresh --------------------------------------------------
    def _refresh_overview(self):
        st = self.state
        cpu_color = ACCENT if st.cpu_mhz >= 240 else (
            PURPLE if st.cpu_mhz >= 160 else GREEN)
        tier = "สูง" if st.cpu_mhz >= 240 else ("กลาง" if st.cpu_mhz >= 160 else "ประหยัด")
        self.card_cpu.set(f"{st.cpu_mhz} MHz" if st.cpu_mhz else "-",
                          f"ระดับ{tier} · ปรับตามภาระงานอัตโนมัติ", cpu_color)
        load_color = GREEN if st.cpu_load < 30 else (
            AMBER if st.cpu_load <= 70 else RED)
        target = 80 if st.cpu_load < 30 else (160 if st.cpu_load <= 70 else 240)
        self.card_load.set(f"{st.cpu_load} %" if st.cpu_mhz else "-",
                           f"เป้าหมาย {target} MHz", load_color)
        self.card_ap.set(st.ap_ssid,
                         f"IP {st.ap_ip} · ลูกข่าย {len(st.clients)}/4 · LED GPIO {st.led_gpio}",
                         GREEN)
        if st.adc_mv is None:
            self.card_sensor.set("-", "ยังไม่มีตัวอย่างจาก ADC", AMBER)
        else:
            self.card_sensor.set(f"{st.adc_mv} mV",
                                 f"ค่าดิบ · q = {st.adc_q}", AMBER)
        if st.adc_q is None:
            self.card_quant.set("-", "ยังไม่มีข้อมูล", CYAN)
        else:
            self.card_quant.set(f"{st.packet_bytes} ไบต์",
                                f"{st.adc_mv} → {st.adc_restored:.1f} mV (คลาด {st.adc_err:.1f})",
                                CYAN)
        if st.uplink_configured is False:
            self.card_uplink.set("ยังไม่ตั้งค่า", "โหมด AP อย่างเดียว", MUTED)
        elif st.sta_connected:
            hb = (f"heartbeat HTTP {st.heartbeat_http}"
                  if st.heartbeat_http is not None else "ยังไม่ส่ง")
            self.card_uplink.set(st.uplink_name, f"IP {st.uplink_ip} · {hb}", GREEN)
        else:
            self.card_uplink.set(st.uplink_name, "กำลังเชื่อมต่อ uplink...", RED)

        led_text, led_reason = st.led_mode
        self.led_card.set(led_text, led_reason,
                          GREEN if led_text == "ติดค้าง" else AMBER)

        details = [f"NAPT: {'เปิด' if st.napt else 'ยังไม่เปิด'}",
                   f"เน็ต: {{'online':'ออนไลน์','offline':'ออฟไลน์'}}.get(st.internet,'ยังไม่ทราบ')"]
        if st.config_http is not None:
            details.append(f"config HTTP {st.config_http}")
        if st.event_http is not None:
            details.append(f"event HTTP {st.event_http}")
        if st.rssi != 0:
            details.append(f"RSSI {st.rssi} dBm")
        if st.free_heap > 0:
            details.append(f"Heap {st.free_heap // 1024}KB")
        self.link_card.set(
            f"ลูกข่าย {len(st.clients)} เครื่อง" if st.clients else "ไม่มีลูกข่ายเชื่อมต่อ",
            " · ".join(details), PURPLE)
        self.siphon_card.set(f"{st.siphon_dispatched} งาน",
                             f"ทยอยส่ง 1 งาน/100ms · คิว {st.siphon_depth}",
                             GREEN)

        self.badge_usb.set("USB เชื่อมต่อ" if self.state.port else "USB",
                           GREEN if self.state.port else DIM)
        self.badge_ap.set(f"AP {st.ap_ssid}" if st.ap_ssid != "-" else "AP",
                          GREEN if st.ap_ssid != "-" else DIM)
        self.badge_uplink.set(
            "Uplink พร้อม" if st.sta_connected else (
                "ไม่ได้ตั้งค่า" if st.uplink_configured is False else "Uplink รอเชื่อมต่อ"),
            GREEN if st.sta_connected else (
                DIM if st.uplink_configured is False else AMBER))
        net_color = GREEN if st.internet == "online" else (
            RED if st.internet == "offline" else DIM)
        self.badge_net.set(
            {"online": "ออนไลน์", "offline": "ออฟไลน์"}.get(st.internet, "ยังไม่ทราบ"),
            net_color)

    def _update_graphs(self):
        st = self.state
        self.chart_cpu.set_points([(t, float(m)) for t, m, _ in st.cpu_points])
        self.chart_load.set_points([(t, float(l)) for t, _, l in st.cpu_points])
        self.chart_adc.set_points([(t, float(mv)) for t, mv, _ in st.sensor_points])
        self.chart_siphon.set_points([(t, float(n)) for t, n in st.siphon_points])

    def _refresh_status(self):
        st = self.state
        lines = []

        def add_header(text):
            lines.append(("header", text + "\n"))

        def add_row(label, value, tag="value"):
            lines.append(("label", f"  {label}: "))
            lines.append((tag, f"{value}\n"))

        def add_sep():
            lines.append(("label", "  " + "─" * 56 + "\n"))

        add_header("═══ สถานะระบบทั้งหมด (FreeNet-X Node) ═══")
        add_sep()
        add_header("📡 การเชื่อมต่อ")
        add_row("พอร์ต USB", st.port or "-")
        add_row("AP SSID", st.ap_ssid)
        add_row("AP IP", st.ap_ip)
        add_row("Uplink SSID", st.uplink_name)
        add_row("Uplink IP", st.uplink_ip)
        add_row("STA เชื่อมต่อ", "ใช่" if st.sta_connected else "ไม่")
        add_row("NAPT", "เปิด" if st.napt else "ยังไม่เปิด")
        add_row("NAPT Error", "มี" if st.napt_error else "ไม่มี",
                "error" if st.napt_error else "ok")
        add_row("DNS Server", st.dns_server)
        add_row("เน็ต", {"online": "ออนไลน์", "offline": "ออฟไลน์"}.get(
            st.internet, "ยังไม่ทราบ"))
        add_row("WiFi Reason", st.wifi_disconnect_reason if st.wifi_disconnect_reason else "-")
        add_row("RSSI", f"{st.rssi} dBm" if st.rssi != 0 else "-")
        add_row("WiFi Channel", st.wifi_channel if st.wifi_channel > 0 else "-")
        add_row("Auth Mode", {0: "OPEN", 1: "WEP", 2: "WPA", 3: "WPA2",
                              4: "WPA3"}.get(st.authmode, "-"))
        add_sep()
        add_header("⚡ CPU / DFS")
        add_row("ความเร็ว CPU", f"{st.cpu_mhz} MHz")
        add_row("ภาระ CPU", f"{st.cpu_load} %")
        add_row("DFS เปิด", "ใช่" if st.dfs_enabled else "ไม่")
        add_row("DFS Tiers", st.dfs_tiers)
        add_row("ระดับ", "สูง" if st.cpu_mhz >= 240 else (
            "กลาง" if st.cpu_mhz >= 160 else "ประหยัด"))
        add_row("Free Heap", f"{st.free_heap} B" if st.free_heap > 0 else "-")
        add_row("Min Heap", f"{st.min_heap} B" if st.min_heap > 0 else "-")
        add_sep()
        add_header("📊 เซนเซอร์ ADC")
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
        add_row("จำนวนที่หลุด", f"{st.siphon_dropped} งาน",
                "warn" if st.siphon_dropped > 0 else "ok")
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
        add_row("Placeholder Uplink", "มี" if st.placeholder_uplink else "ไม่มี",
                "warn" if st.placeholder_uplink else "ok")
        add_row("Placeholder Token", "มี" if st.placeholder_token else "ไม่มี",
                "warn" if st.placeholder_token else "ok")
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
        add_sep()

        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        for tag, text in lines:
            self.status_text.insert("end", text, tag)
        self.status_text.configure(state="disabled")

    def _show_fleet(self, data):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for n in data.get("nodes", []):
            self.tree.insert("", "end", values=(
                n.get("node_id", "-"),
                "ออนไลน์" if n.get("online") else "ออฟไลน์",
                n.get("rssi") if n.get("rssi") is not None else "-",
                n.get("ip_address") or "-",
                n.get("firmware") or "-",
                f"{int(n.get('age_seconds', 0))} วินาที",
            ))
        self.server_note.configure(
            text=f"พบ {len(data.get('nodes', []))} โหนด · "
                 f"เวลาเซิร์ฟเวอร์ {data.get('server_time', '-')}",
            fg=MUTED)

    def _demo_tick(self):
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
        st.config_http = 200
        st.event_http = 200
        if n == 2:
            st.clients["AA:BB:CC:DD:EE:01"] = "192.168.4.2"
        load = int(45 + 40 * abs((n % 30) - 15) / 15)
        st.cpu_load = load
        st.cpu_mhz = 240 if load > 70 else (160 if load >= 30 else 80)
        now = time.monotonic()
        st.cpu_points.append((now, st.cpu_mhz, load))
        st.adc_mv = 1200 + int(600 * (1 + math.sin(n / 5)) / 2)
        st.adc_q = round(st.adc_mv / 3300 * 255) - 128
        st.adc_restored = ((st.adc_q + 128) / 255) * 3300
        st.adc_err = abs(st.adc_mv - st.adc_restored)
        st.packet_bytes = 6
        st.sensor_points.append((now, st.adc_mv, st.adc_restored))
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
        self._update_graphs()
        if self.active_tab == "status":
            self._refresh_status()
        self.root.after(900, self._demo_tick)

    def on_close(self):
        self.stop.set()
        if self.state.port:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.state.port)
            path = os.path.join(tempfile.gettempdir(),
                                f"freenetx-monitor-{safe}.lock")
            try:
                os.remove(path)
            except OSError:
                pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def headless(port, baud, seconds):
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
    except Exception as exc:
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
                print(f"  >> CPU {st.cpu_mhz}MHz โหลด {st.cpu_load}% | "
                      f"ADC {st.adc_mv}mV q={st.adc_q} | "
                      f"AP {st.ap_ssid}/{st.ap_ip} | "
                      f"RSSI {st.rssi}dBm | Heap {st.free_heap}B | "
                      f"เน็ต {st.internet} | อัปไทม์ {st.uptime_s}s")
    finally:
        ser.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="FreeNet-X Unified Monitor")
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--server", default="")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--headless", type=int, metavar="SECONDS")
    args = ap.parse_args()

    if args.headless:
        return headless(args.port, args.baud, args.headless)

    root = tk.Tk()
    app = MonitorApp(root, args)
    if not args.demo:
        if not args.server:
            root.after(400, app.connect_serial)
        else:
            root.after(400, app.connect_server)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
