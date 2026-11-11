@echo off
rem FreeNet-X PC monitor launcher (serial COM3 by default).
rem Double-click this file, or run:  pc_monitor.bat --port COM5
cd /d "%~dp0"
set PYTHONUTF8=1
start "" pythonw "%~dp0pc_monitor.py" %*
