#!/usr/bin/env python3
"""FreeNet-X Beautiful Graph Dashboard (Thai UI).

โปรแกรมแสดงผลกราฟแบบ Real-time ด้วยธีมมืดทันสมัย
สำหรับบอร์ด FreeNet-X ESP32

    python graph_dashboard.py                  # GUI, serial COM3
    python graph_dashboard.py --demo           # ข้อมูลจำลอง
    python graph_dashboard.py --port COM5      # พอร์ตอื่น
"""
from __future__ import annotations

import argparse
import math
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field

sys.path.insert(0, r"C:\Users\aisim\Desktop\FreeNetX\FreeNetX_Autonomous_Intelligence_V2\tools")
try:
    from pc_monitor import parse_line, NodeState, clean_line
except ImportError:
    import os
    os.chdir(r"C:\Users\aisim\Desktop\FreeNetX\FreeNetX_Autonomous_Intelligence_V2\tools")
    from pc_monitor import parse_line, NodeState, clean_line

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

# ---------------------------------------------------------------------------
# Theme
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
        families = set(tk.font.families(root))
    except Exception:
        return "TkDefaultFont"
    for name in ("Leelawadee UI", "Leelawadee", "Sarabun", "Noto Sans Thai",
                 "Segoe UI", "Tahoma"):
        if name in families:
            return name
    return "TkDefaultFont"


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


# ---------------------------------------------------------------------------
# Beautiful animated graph canvas
# ---------------------------------------------------------------------------
class BeautifulGraph(tk.Canvas):
    """กราฟแบบ animated ด้วย gradient, glow, และ smooth lines"""

    def __init__(self, parent, title, subtitle="", color=ACCENT,
                 y_min=None, y_max=None, unit="", height=200):
        super().__init__(parent, bg=CARD, highlightthickness=0, bd=0,
                         height=height)
        self.title = title
        self.subtitle = subtitle
        self.color = color
        self.y_min = y_min
        self.y_max = y_max
        self.unit = unit
        self.points = deque(maxlen=MAX_POINTS)
        self._glow_phase = 0
        self.bind("<Configure>", lambda e: self._draw())

    def set_points(self, pts):
        self.points = deque(pts, maxlen=MAX_POINTS)
        self._draw()

    def add_point(self, t, v):
        self.points.append((t, v))
        if len(self.points) > MAX_POINTS:
            self.points.popleft()
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 50 or h < 50:
            return

        # Background gradient effect
        for i in range(0, h, 2):
            ratio = i / h
            r = int(15 + ratio * 5)
            g = int(26 + ratio * 8)
            b = int(46 + ratio * 12)
            self.create_line(0, i, w, i, fill=f"#{r:02x}{g:02x}{b:02x}")

        # Title
        self.create_text(16, 14, text=self.title, anchor="w", fill=FG,
                         font=fnt(12, "bold"))
        if self.subtitle:
            self.create_text(16, 34, text=self.subtitle, anchor="w",
                             fill=MUTED, font=fnt(9))

        left, right, top, bottom = 50, 16, 52, 24
        plot_w = w - left - right
        plot_h = h - top - bottom
        if plot_w < 20 or plot_h < 20:
            return

        pts = list(self.points)
        if not pts:
            self.create_text(w // 2, h // 2, text="กำลังรอข้อมูล...",
                             fill=DIM, font=fnt(11))
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

        # Grid lines with labels
        for i in range(5):
            val = lo + (hi - lo) * i / 4
            y = y_of(val)
            self.create_line(left, y, left + plot_w, y, fill="#1a2d4a",
                             dash=(2, 4))
            self.create_text(left - 8, y, text=f"{val:.0f}", anchor="e",
                             fill=DIM, font=fnt(8))

        # Axis labels
        self.create_text(left, top + plot_h + 14, text=f"-{span:.0f}s",
                         anchor="w", fill=DIM, font=fnt(8))
        self.create_text(left + plot_w, top + plot_h + 14, text="ล่าสุด",
                         anchor="e", fill=DIM, font=fnt(8))
        if self.unit:
            self.create_text(left + plot_w + 4, top - 4, text=self.unit,
                             anchor="w", fill=MUTED, font=fnt(8, "bold"))

        # Glow effect (shadow line)
        coords = []
        for t, v in pts:
            coords.extend((x_of(t), y_of(v)))
        if len(coords) >= 4:
            # Shadow
            self.create_line(*coords, fill=self.color, width=6,
                             smooth=True, splinesteps=12)
            # Main line
            self.create_line(*coords, fill=self.color, width=2.5,
                             smooth=True, splinesteps=12)

        # Dots on last few points
        for t, v in pts[-8:]:
            x, y = x_of(t), y_of(v)
            self.create_oval(x - 3, y - 3, x + 3, y + 3,
                             fill=self.color, outline=BG, width=1.5)

        # Current value badge
        if pts:
            last_val = pts[-1][1]
            badge_x = left + plot_w - 4
            badge_y = top + 4
            self.create_rectangle(badge_x - 50, badge_y - 12,
                                  badge_x + 4, badge_y + 12,
                                  fill=self.color, outline="")
            self.create_text(badge_x - 23, badge_y, text=f"{last_val:.0f}",
                             fill="#050a14", font=fnt(10, "bold"))

        # Subtle border
        self.create_rectangle(1, 1, w - 1, h - 1, outline=BORDER, width=1)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class Dashboard:
    def __init__(self, root, args):
        global THAI_FONT
        self.root = root
        self.args = args
        self.state = NodeState()
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.reader = None
        self._demo_n = 0

        THAI_FONT = pick_font(root)
        root.title("FreeNet-X · กราฟแสดงผลแบบ Real-time")
        root.configure(bg=BG)
        root.geometry("1280x800")
        root.minsize(1000, 700)

        self._build_header()
        self._build_graphs()
        self._build_status_bar()

        self.source_var = tk.StringVar(
            value="server" if args.server else "serial")
        if args.demo:
            root.after(400, self._demo_tick)
        elif args.server:
            pass
        else:
            root.after(400, self._connect_serial)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._drain)
        root.after(1000, self._tick)

    def _build_header(self):
        header = tk.Frame(self.root, bg=SURFACE, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)

        # Logo
        canvas = tk.Canvas(header, width=36, height=36, bg=SURFACE,
                           highlightthickness=0)
        canvas.pack(side="left", padx=(20, 12), pady=10)
        canvas.create_rectangle(2, 2, 34, 34, fill=ACCENT, outline="")
        canvas.create_text(18, 18, text="FX", fill="#050a14",
                           font=fnt(12, "bold"))

        tk.Label(header, text="FreeNet-X · กราฟแสดงผลแบบ Real-time",
                 bg=SURFACE, fg=FG, font=fnt(14, "bold")).pack(side="left",
                                                               pady=10)
        tk.Label(header, text="DFS · Siphon Queue · Sensor · Link Status",
                 bg=SURFACE, fg=MUTED, font=fnt(9)).pack(side="left",
                                                          padx=(12, 0),
                                                          pady=10)

        # Status indicator
        self.status_dot = tk.Canvas(header, width=14, height=14, bg=SURFACE,
                                    highlightthickness=0)
        self.status_dot.pack(side="right", padx=(0, 20), pady=21)
        self.dot_id = self.status_dot.create_oval(2, 2, 12, 12, fill=DIM,
                                                  outline="")
        self.status_lbl = tk.Label(header, text="ยังไม่เชื่อมต่อ", bg=SURFACE,
                                   fg=MUTED, font=fnt(9))
        self.status_lbl.pack(side="right", pady=21)

    def _build_graphs(self):
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=16, pady=(8, 4))
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        self.g_cpu = BeautifulGraph(
            container, "ความเร็ว CPU (MHz)", "DFS: 80 → 160 → 240 MHz",
            color=ACCENT, y_min=60, y_max=260, unit="MHz", height=180)
        self.g_cpu.grid(row=0, column=0, sticky="nsew", padx=(0, 6),
                        pady=(0, 6))

        self.g_load = BeautifulGraph(
            container, "ภาระ CPU (%)", "Threshold: 30% / 70%",
            color=PURPLE, y_min=0, y_max=100, unit="%", height=180)
        self.g_load.grid(row=0, column=1, sticky="nsew", padx=(6, 0),
                         pady=(0, 6))

        self.g_adc = BeautifulGraph(
            container, "เซนเซอร์ ADC (mV)", "ค่าจริง vs แปลงกลับ",
            color=AMBER, y_min=0, y_max=3500, unit="mV", height=180)
        self.g_adc.grid(row=1, column=0, sticky="nsew", padx=(0, 6),
                        pady=(6, 0))

        self.g_siphon = BeautifulGraph(
            container, "Siphon Queue (งาน)", "ทยอยส่งให้ PC",
            color=GREEN, y_min=0, y_max=None, unit="งาน", height=180)
        self.g_siphon.grid(row=1, column=1, sticky="nsew", padx=(6, 0),
                           pady=(6, 0))

    def _build_status_bar(self):
        bar = tk.Frame(self.root, bg=SURFACE, height=48)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self.status_items = {}
        items = [
            ("cpu", "CPU", ACCENT),
            ("load", "ภาระ", PURPLE),
            ("adc", "ADC", AMBER),
            ("link", "ลิงก์", GREEN),
            ("siphon", "Siphon", CYAN),
            ("uptime", "อัปไทม์", MUTED),
        ]
        for key, label, color in items:
            frame = tk.Frame(bar, bg=SURFACE)
            frame.pack(side="left", padx=16, pady=8)
            tk.Label(frame, text=label, bg=SURFACE, fg=MUTED,
                     font=fnt(8, "bold")).pack(anchor="w")
            lbl = tk.Label(frame, text="-", bg=SURFACE, fg=color,
                           font=fnt(12, "bold"))
            lbl.pack(anchor="w")
            self.status_items[key] = lbl

    def _connect_serial(self):
        if serial is None:
            return
        self.state = NodeState()
        self.stop.clear()
        self.reader = SerialReader(self.args.port, self.args.baud,
                                  self.queue, self.stop)
        self.reader.start()

    def _drain(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    changed = parse_line(payload, self.state)
                    if changed:
                        self._update_graphs()
                elif kind == "status":
                    if payload.startswith("เชื่อมต่อแล้ว"):
                        self.status_lbl.configure(text=payload, fg=GREEN)
                        self.status_dot.itemconfigure(self.dot_id, fill=GREEN)
                    elif payload.startswith("ตัดการเชื่อมต่อ"):
                        self.status_lbl.configure(text=payload, fg=MUTED)
                        self.status_dot.itemconfigure(self.dot_id, fill=DIM)
                    else:
                        self.status_lbl.configure(text=payload, fg=RED)
                        self.status_dot.itemconfigure(self.dot_id, fill=RED)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _update_graphs(self):
        st = self.state
        now = time.monotonic()

        # CPU frequency points
        cpu_pts = [(t, float(mhz)) for t, mhz, _ in st.cpu_points]
        self.g_cpu.set_points(cpu_pts)

        # CPU load points
        load_pts = [(t, float(load)) for t, _, load in st.cpu_points]
        self.g_load.set_points(load_pts)

        # ADC points
        adc_pts = [(t, float(mv)) for t, mv, _ in st.sensor_points]
        self.g_adc.set_points(adc_pts)

        # Siphon points
        siphon_pts = [(t, float(n)) for t, n in st.siphon_points]
        self.g_siphon.set_points(siphon_pts)

        # Update status bar
        self.status_items["cpu"].configure(
            text=f"{st.cpu_mhz} MHz" if st.cpu_mhz else "-")
        self.status_items["load"].configure(
            text=f"{st.cpu_load} %" if st.cpu_mhz else "-")
        self.status_items["adc"].configure(
            text=f"{st.adc_mv} mV" if st.adc_mv is not None else "-")
        link_text = {100: "ออนไลน์", 60: "uplink", 20: "หลุด"}.get(
            st.link_level, "-")
        self.status_items["link"].configure(text=link_text)
        self.status_items["siphon"].configure(
            text=f"{st.siphon_dispatched}")
        self.status_items["uptime"].configure(
            text=f"{st.uptime_s}s")

    def _tick(self):
        if self.args.demo:
            pass
        self._update_graphs()
        self.root.after(1000, self._tick)

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
        st.free_heap = 240000 + (n % 5000)
        st.min_heap = 230000
        st.rssi = -58 + (n % 10)
        st.wifi_channel = 6
        st.heartbeat_count += 1
        self._update_graphs()
        self.root.after(900, self._demo_tick)

    def _on_close(self):
        self.stop.set()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="FreeNet-X Beautiful Graph Dashboard")
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--server", default="")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    root = tk.Tk()
    app = Dashboard(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
