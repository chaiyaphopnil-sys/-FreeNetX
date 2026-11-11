@echo off
rem Starts the hidden USB watcher: opens the dashboard when the ESP32 is plugged in.
rem Use "auto_monitor.py --install" once to start this at Windows login.
cd /d "%~dp0"
set PYTHONUTF8=1
start "" pythonw "%~dp0auto_monitor.py" %*
