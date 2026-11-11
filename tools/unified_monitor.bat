@echo off
rem FreeNet-X Unified Monitor launcher (all-in-one).
rem Double-click to open the unified display with beautiful graphs.
cd /d "%~dp0"
set PYTHONUTF8=1
start "" pythonw "%~dp0unified_monitor.py" %*
