@echo off
rem Launch the booth minimized. Double-click for a league night; close the "Golf Booth" window to stop.
rem Running it again replaces the booth: any booth already running (and its window) is closed first.
cd /d "%~dp0"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'commentator run' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
start "Golf Booth" /min cmd /k .venv\Scripts\python.exe -m commentator run
