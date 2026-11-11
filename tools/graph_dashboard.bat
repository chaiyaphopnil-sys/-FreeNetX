@echo off
rem FreeNet-X Beautiful Graph Dashboard launcher.
rem Double-click to open the real-time graph display.
cd /d "%~dp0"
set PYTHONUTF8=1
start "" pythonw "%~dp0graph_dashboard.py" %*
