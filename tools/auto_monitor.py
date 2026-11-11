#!/usr/bin/env python3
"""FreeNet-X auto monitor.

Watches the USB serial ports and opens the PC dashboard automatically when
the ESP32 is plugged in. When the board is unplugged, the dashboard it
launched is closed again. A second instance refuses to start.

    python auto_monitor.py --once       # list what would be detected
    python auto_monitor.py              # run the watcher (console)
    python auto_monitor.py --install    # start automatically at Windows login
    python auto_monitor.py --uninstall  # remove the login entry
"""
from __future__ import annotations

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
MONITOR = TOOLS_DIR / "unified_monitor.py"
LOG_FILE = Path(tempfile.gettempdir()) / "freenetx-auto-monitor.log"
SINGLETON_PORT = 47654
CREATE_NO_WINDOW = 0x08000000

# USB IDs of common ESP32 dev-board UART bridges:
# CP210x, CH340/CH341, FTDI, and the native USB of newer ESP32 chips.
ESP_IDS = {
    (0x10C4, 0xEA60),  # Silicon Labs CP2102/CP2104
    (0x1A86, 0x7523),  # QinHeng CH340
    (0x1A86, 0x5523),  # QinHeng CH341
    (0x0403, 0x6001),  # FTDI FT232
    (0x303A, 0x1001),  # Espressif USB JTAG/serial
    (0x303A, 0x0002),
    (0x303A, 0x1002),
}
ESP_TEXT = ("CP210", "CH340", "CH341", "SILICON LABS", "FTDI", "ESP32")

sys.path.insert(0, str(TOOLS_DIR))
from pc_monitor import monitor_lock_path  # noqa: E402


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def is_esp_port(info) -> bool:
    if info.vid is not None and info.pid is not None:
        if (info.vid, info.pid) in ESP_IDS:
            return True
    text = f"{info.description or ''} {info.hwid or ''}".upper()
    return any(token in text for token in ESP_TEXT)


def find_ports(forced: str | None) -> list[str]:
    from serial.tools import list_ports

    found = []
    for info in list_ports.comports():
        if forced:
            if info.device.upper() == forced.upper():
                found.append(info.device)
        elif is_esp_port(info):
            found.append(info.device)
    return sorted(found)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def monitor_busy(port: str) -> bool:
    path = Path(monitor_lock_path(port))
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    if pid_alive(pid):
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def launch_monitor(port: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [pythonw(), str(MONITOR), "--port", port],
        cwd=str(TOOLS_DIR),
        creationflags=CREATE_NO_WINDOW,
    )
    log(f"launched monitor for {port} (pid {proc.pid})")
    return proc


def acquire_singleton():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLETON_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def watch(args: argparse.Namespace) -> int:
    guard = acquire_singleton()
    if guard is None:
        log("another auto-monitor is already running; exiting")
        return 0

    log("auto-monitor started")
    procs: dict[str, subprocess.Popen] = {}
    seen: set[str] = set()
    first_scan = True

    while True:
        current = set(find_ports(args.port))

        for port in sorted(current - seen):
            if monitor_busy(port):
                log(f"{port} is already open in a monitor window; not launching")
                continue
            if first_scan and args.no_launch_existing:
                log(f"{port} already present; waiting for the next plug-in")
                continue
            log(f"USB serial device detected: {port}")
            if args.no_launch:
                continue
            time.sleep(args.delay)
            procs[port] = launch_monitor(port)
        first_scan = False

        for port in sorted(seen - current):
            proc = procs.pop(port, None)
            log(f"{port} unplugged")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log(f"closed monitor for {port}")

        for port, proc in list(procs.items()):
            if proc.poll() is not None:
                procs.pop(port, None)

        seen = current
        time.sleep(args.interval)


def startup_shortcut() -> Path:
    startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" /
               "Start Menu" / "Programs" / "Startup")
    return startup / "FreeNetX Auto Monitor.lnk"


def install() -> int:
    startup_shortcut().parent.mkdir(parents=True, exist_ok=True)
    lnk = startup_shortcut()
    script = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$sc = $ws.CreateShortcut('{lnk}')\n"
        f"$sc.TargetPath = '{pythonw()}'\n"
        f"$sc.Arguments = '\"{MONITOR}\"'\n"
        f"$sc.WorkingDirectory = '{TOOLS_DIR}'\n"
        "$sc.WindowStyle = 7\n"
        "$sc.Description = 'FreeNet-X auto monitor'\n"
        "$sc.Save()\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        log("could not create the startup shortcut")
        return 1
    log(f"installed: {lnk}")
    log("the monitor will now start automatically at Windows login")
    return 0


def uninstall() -> int:
    try:
        startup_shortcut().unlink()
        log(f"removed: {startup_shortcut()}")
    except FileNotFoundError:
        log("startup entry was not installed")
    except OSError as exc:
        log(f"could not remove startup entry: {exc}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeNet-X USB auto monitor")
    ap.add_argument("--port", help="only watch this port (default: any ESP32 serial)")
    ap.add_argument("--interval", type=float, default=2.0, help="scan interval (s)")
    ap.add_argument("--delay", type=float, default=1.5, help="wait after plug-in (s)")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--no-launch", action="store_true", help="detect only, never launch")
    ap.add_argument("--no-launch-existing", action="store_true",
                    help="ignore devices already plugged in at start")
    ap.add_argument("--install", action="store_true", help="start at Windows login")
    ap.add_argument("--uninstall", action="store_true", help="remove login entry")
    args = ap.parse_args()

    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.once:
        ports = find_ports(args.port)
        print("detected:", ", ".join(ports) if ports else "(none)")
        return 0

    try:
        return watch(args)
    except KeyboardInterrupt:
        log("auto-monitor stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
