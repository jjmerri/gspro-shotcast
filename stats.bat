@echo off
rem Open the stats page (the season database in logs\season.db) in your browser. Close this window to stop it.
cd /d "%~dp0"
title Shotcast stats
.venv\Scripts\python.exe -m commentator stats-web
pause
